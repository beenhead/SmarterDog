#!/usr/bin/env python3
"""
SmarterDog recording viewer.

Indexes the recorder's 10 minute .mkv segments (motion profile + animated
thumbnail) and serves a small web UI to browse and play them.
Only needs the Python standard library and ffmpeg.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

NAME_RE = re.compile(r"^(\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d)\.mkv$")
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))

# Motion analysis runs on a tiny grayscale copy of the video
ANALYSIS_FPS = 2
ANALYSIS_W, ANALYSIS_H = 64, 36
PIXEL_THRESHOLD = 12  # grey level change that counts as a changed pixel
# Ignore the top third of the picture: it holds the burnt-in clock and, from a floor level
# camera, mostly windows and ceiling where sunlight changes look like motion
IGNORE_TOP_ROWS = ANALYSIS_H // 3
# Default motion threshold (changed fraction per second); the UI can override it
DEFAULT_MOTION = 0.004
# More than this fraction changing at once is a lighting change (IR switching), not motion
LIGHTING_CHANGE = 0.5

THUMB_FRAMES = 12
THUMB_WIDTH = 320
MP4_CACHE_FILES = 4
# The newest file is still being written until it has been idle this long
SETTLE_SECONDS = 60


def log(message):
	print(message, flush=True)


def parse_start(stem):
	return datetime.strptime(stem, "%Y-%m-%d_%H-%M-%S").replace(tzinfo=timezone.utc)


class Indexer:
	def __init__(self, recordings, index):
		self.recordings = recordings
		self.index = index
		os.makedirs(index, exist_ok=True)
		self.lock = threading.Lock()
		self.mp4_lock = threading.Lock()

	def segments(self):
		"""Return [(stem, path, stat)] sorted oldest first."""
		out = []
		for name in os.listdir(self.recordings):
			match = NAME_RE.match(name)
			if match:
				path = os.path.join(self.recordings, name)
				try:
					out.append((match.group(1), path, os.stat(path)))
				except FileNotFoundError:
					pass
		out.sort()
		return out

	def is_settled(self, stem, stat, newest_stem):
		return stem != newest_stem or time.time() - stat.st_mtime > SETTLE_SECONDS

	def meta_path(self, stem):
		return os.path.join(self.index, stem + ".json")

	def thumb_path(self, stem):
		return os.path.join(self.index, stem + ".webp")

	def mp4_path(self, stem):
		return os.path.join(self.index, stem + ".mp4")

	def load_meta(self, stem):
		try:
			with open(self.meta_path(stem)) as f:
				return json.load(f)
		except (FileNotFoundError, json.JSONDecodeError):
			return None

	# ---------------------------------------------------------------- analysis

	def analyse(self, stem, path):
		frame_size = ANALYSIS_W * ANALYSIS_H
		pixels = range(IGNORE_TOP_ROWS * ANALYSIS_W, frame_size)
		proc = subprocess.Popen(
			["nice", "-n", "15", "ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", path,
			 "-vf", f"fps={ANALYSIS_FPS},scale={ANALYSIS_W}:{ANALYSIS_H},format=gray",
			 "-f", "rawvideo", "-"],
			stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
		changes = []
		previous = None
		while True:
			frame = proc.stdout.read(frame_size)
			if len(frame) < frame_size:
				break
			if previous is not None:
				# Compensate for exposure changes by removing the average brightness shift
				shift = sum(frame[i] - previous[i] for i in pixels) / len(pixels)
				changed = sum(1 for i in pixels if abs(frame[i] - previous[i] - shift) > PIXEL_THRESHOLD)
				changes.append(changed / len(pixels))
			previous = frame
		proc.wait()
		# One value per second: the largest change seen in that second
		per_second = []
		for i in range(0, len(changes), ANALYSIS_FPS):
			per_second.append(max(changes[i:i + ANALYSIS_FPS]))
		# Stored as parts per ten thousand to keep the JSON small
		motion = [min(10000, round(v * 10000)) for v in per_second]
		return motion

	@staticmethod
	def events(motion, threshold=DEFAULT_MOTION, gap=5, min_length=1):
		"""Group seconds above threshold into [start, end] events, merging gaps up to `gap` seconds."""
		limit = threshold * 10000
		lighting = LIGHTING_CHANGE * 10000
		found = []
		for second, value in enumerate(motion):
			if limit <= value < lighting:
				if found and second - found[-1][1] <= gap:
					found[-1][1] = second
				else:
					found.append([second, second])
		return [e for e in found if e[1] - e[0] + 1 >= min_length]

	def thumbnail(self, stem, path, duration, motion):
		"""Animated WebP; favours the busiest moments when there is motion."""
		evts = self.events(motion)
		if evts:
			# Spread frames over the events, weighted by their length
			moments = []
			for start, end in evts:
				moments.extend(range(start, end + 1))
			step = max(1, len(moments) / THUMB_FRAMES)
			times = [moments[int(i * step)] for i in range(min(THUMB_FRAMES, len(moments)))]
		else:
			times = [duration * (i + 0.5) / THUMB_FRAMES for i in range(THUMB_FRAMES)]
		work = tempfile.mkdtemp(prefix="sdthumb-")
		try:
			count = 0
			for t in times:
				out = os.path.join(work, f"f{count:02d}.jpg")
				subprocess.run(
					["nice", "-n", "15", "ffmpeg", "-hide_banner", "-nostdin", "-v", "error",
					 "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
					 "-vf", f"scale={THUMB_WIDTH}:-2", "-q:v", "4", "-y", out],
					stderr=subprocess.DEVNULL, check=False)
				if os.path.exists(out):
					count += 1
			if count == 0:
				return False
			tmp = self.thumb_path(stem) + ".tmp.webp"
			subprocess.run(
				["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-framerate", "2.5",
				 "-i", os.path.join(work, "f%02d.jpg"), "-c:v", "libwebp_anim", "-loop", "0",
				 "-quality", "60", "-y", tmp],
				stderr=subprocess.DEVNULL, check=False)
			if os.path.exists(tmp):
				os.replace(tmp, self.thumb_path(stem))
				return True
			return False
		finally:
			shutil.rmtree(work, ignore_errors=True)

	def process(self, stem, path):
		started = time.time()
		motion = self.analyse(stem, path)
		duration = len(motion)
		if duration == 0:
			# Nothing decodable; remember that so we do not retry forever
			meta = {"stem": stem, "duration": 0, "motion": [], "error": "no video"}
		else:
			self.thumbnail(stem, path, duration, motion)
			meta = {"stem": stem, "duration": duration, "motion": motion}
		meta["size"] = os.path.getsize(path)
		meta["version"] = 2
		tmp = self.meta_path(stem) + ".tmp"
		with open(tmp, "w") as f:
			json.dump(meta, f, separators=(",", ":"))
		os.replace(tmp, self.meta_path(stem))
		log(f"indexed {stem}: {duration}s, {len(self.events(motion))} motion event(s) in {time.time() - started:.1f}s")

	def prune(self, stems):
		"""Remove index files for recordings that have been deleted."""
		for name in os.listdir(self.index):
			stem = name.split(".")[0]
			if stem not in stems:
				try:
					os.remove(os.path.join(self.index, name))
				except FileNotFoundError:
					pass

	def run_once(self):
		segments = self.segments()
		if not segments:
			self.prune(set())
			return
		newest = segments[-1][0]
		self.prune(set(s for s, _, _ in segments))
		# Newest first, so fresh recordings show up quickly
		for stem, path, stat in reversed(segments):
			if not self.is_settled(stem, stat, newest):
				continue
			meta = self.load_meta(stem)
			if meta is not None and meta.get("size") == stat.st_size and meta.get("version") == 2:
				continue
			try:
				self.process(stem, path)
			except Exception as e:  # keep indexing the rest
				log(f"failed to index {stem}: {e}")

	def run_forever(self, interval=30):
		while True:
			try:
				self.run_once()
			except Exception as e:
				log(f"indexer error: {e}")
			time.sleep(interval)

	# ---------------------------------------------------------------- API data

	def listing(self):
		segments = self.segments()
		newest = segments[-1][0] if segments else None
		items = []
		for stem, path, stat in reversed(segments):
			start = parse_start(stem)
			item = {"stem": stem, "start": start.isoformat().replace("+00:00", "Z"), "size": stat.st_size}
			if not self.is_settled(stem, stat, newest):
				item["status"] = "recording"
				item["duration"] = max(0, int(time.time() - start.timestamp()))
			else:
				meta = self.load_meta(stem)
				if meta is None or meta.get("size") != stat.st_size:
					item["status"] = "processing"
					item["duration"] = None
				else:
					item["status"] = "error" if meta.get("error") else "ready"
					item["duration"] = meta["duration"]
					item["motion"] = meta["motion"]
					item["thumb"] = os.path.exists(self.thumb_path(stem))
			items.append(item)
		return {"recordings": items, "defaultThreshold": DEFAULT_MOTION, "lightingChange": LIGHTING_CHANGE,
				"now": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}

	def mp4(self, stem):
		"""Remux a recording to MP4 (no re-encode) so browsers can play and seek it."""
		source = os.path.join(self.recordings, stem + ".mkv")
		target = self.mp4_path(stem)
		with self.mp4_lock:
			if os.path.exists(target) and os.path.getmtime(target) >= os.path.getmtime(source):
				os.utime(target)
				return target
			tmp = target + ".tmp.mp4"
			subprocess.run(
				["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", source, "-map", "0:v",
				 "-c", "copy", "-movflags", "+faststart", "-y", tmp],
				stderr=subprocess.DEVNULL, check=True)
			os.replace(tmp, target)
			# Keep only the most recently used conversions
			cached = sorted((os.path.getmtime(os.path.join(self.index, n)), n)
							for n in os.listdir(self.index) if n.endswith(".mp4") and ".tmp" not in n)
			for _, name in cached[:-MP4_CACHE_FILES]:
				os.remove(os.path.join(self.index, name))
			return target


class Handler(SimpleHTTPRequestHandler):
	indexer = None

	def log_message(self, fmt, *args):
		pass

	def send_json(self, data):
		body = json.dumps(data, separators=(",", ":")).encode()
		self.send_response(HTTPStatus.OK)
		self.send_header("Content-Type", "application/json")
		self.send_header("Cache-Control", "no-store")
		self.send_header("Content-Length", str(len(body)))
		self.end_headers()
		self.wfile.write(body)

	def send_file(self, path, content_type, cache=True):
		"""Serve a file with HTTP Range support (needed for video seeking)."""
		try:
			size = os.path.getsize(path)
			f = open(path, "rb")
		except FileNotFoundError:
			self.send_error(HTTPStatus.NOT_FOUND)
			return
		with f:
			start, end = 0, size - 1
			status = HTTPStatus.OK
			ranged = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", ""))
			if ranged and size > 0:
				if ranged.group(1):
					start = int(ranged.group(1))
					if ranged.group(2):
						end = min(int(ranged.group(2)), size - 1)
				elif ranged.group(2):
					start = max(0, size - int(ranged.group(2)))
				if start > end:
					self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
					self.send_header("Content-Range", f"bytes */{size}")
					self.end_headers()
					return
				status = HTTPStatus.PARTIAL_CONTENT
			self.send_response(status)
			self.send_header("Content-Type", content_type)
			self.send_header("Accept-Ranges", "bytes")
			self.send_header("Content-Length", str(end - start + 1))
			self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
			if status == HTTPStatus.PARTIAL_CONTENT:
				self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
			self.end_headers()
			f.seek(start)
			remaining = end - start + 1
			try:
				while remaining > 0:
					chunk = f.read(min(256 * 1024, remaining))
					if not chunk:
						break
					self.wfile.write(chunk)
					remaining -= len(chunk)
			except (BrokenPipeError, ConnectionResetError):
				pass

	def do_GET(self):
		path = unquote(urlparse(self.path).path)
		if path in ("/", "/index.html"):
			self.send_file(os.path.join(STATIC_DIR, "index.html"), "text/html; charset=utf-8", cache=False)
		elif path == "/api/recordings":
			self.send_json(self.indexer.listing())
		elif m := re.match(r"^/thumb/(\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d)\.webp$", path):
			self.send_file(self.indexer.thumb_path(m.group(1)), "image/webp")
		elif m := re.match(r"^/video/(\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d)\.mp4$", path):
			stem = m.group(1)
			if not os.path.exists(os.path.join(self.indexer.recordings, stem + ".mkv")):
				self.send_error(HTTPStatus.NOT_FOUND)
				return
			try:
				target = self.indexer.mp4(stem)
			except subprocess.CalledProcessError:
				self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "conversion failed")
				return
			self.send_file(target, "video/mp4")
		else:
			self.send_error(HTTPStatus.NOT_FOUND)

	def do_HEAD(self):
		self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)


def main():
	parser = argparse.ArgumentParser(description="SmarterDog recording viewer")
	parser.add_argument("--recordings", default="/srv/smarterdog")
	parser.add_argument("--index", help="where thumbnails and motion data are kept (default: <recordings>/.index)")
	parser.add_argument("--bind", default="0.0.0.0")
	parser.add_argument("--port", type=int, default=8090)
	parser.add_argument("--index-once", action="store_true", help="index pending recordings and exit")
	args = parser.parse_args()

	indexer = Indexer(args.recordings, args.index or os.path.join(args.recordings, ".index"))
	if args.index_once:
		indexer.run_once()
		return
	threading.Thread(target=indexer.run_forever, daemon=True).start()
	Handler.indexer = indexer
	server = ThreadingHTTPServer((args.bind, args.port), Handler)
	log(f"SmarterDog viewer on http://{args.bind}:{args.port}/ serving {args.recordings}")
	server.serve_forever()


if __name__ == "__main__":
	main()
