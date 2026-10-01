# SmarterDog
Backend for CleverDog cloud CCTV

## Introduction

Back in 2017, I wrote a report on reverse engineering a WiFi camera of dubious quality (see https://eriknl.github.io/reverse-engineering/2017/09/07/WiFi-camera.html for the full story).
After concluding this thing was certainly not safe to use with its default cloud backend I created a proof of concept implementation of the protocols involved and managed to convert the video stream into a standard RTSP stream. From this proof of concept I then created this somewhat cleaner code base and I have been using this ever since to use the camera.
In the years since then I have had some people asking me to help them with their own devices, I hope this repository is useful to those that want to try and recreate the backend and have fun with their cameras.

## Disclaimer

Please understand this software is just a proof of concept and I am putting this out there with no guarantees it will even work for your device or for more recent firmware versions.

## How to build and use

Since this application has to run 24/7 make sure to clone the repository to the device you will be running it off of. I have had success with both x86_64 CentOS and a Raspberry Pi 3.
Make sure you have the required build environment, you will need Qt with network support (no gui). Then run `qmake` and finally build with `make`.
Since this is application is not a proper forking daemon you will need to create a script to run it through systemd or your init system of choice (on a Raspberry Pi you could start it from `/etc/rc.local`)

You can scan for cameras in your local network with the `-s` switch, it will list the IP address of any detected cameras along with their CID. After obtaining a valid CID you can then start streaming it with the `-S` switch followed by a comma separated list of CIDs.
When running in streaming mode you can access the stream for a given CID with `rtsp://<yourhost>:8086/CID=<yourcid>`.

## Running as a service and recording

The `systemd/` directory contains units to keep SmarterDog running and to record the last 24 hours of video. Edit the `User=` and the camera CID (`-c` / `CID=`) to match your setup before installing.

* `smarterdog.service` runs the RTSP server at boot and restarts it if it exits. It is granted `CAP_NET_BIND_SERVICE` so the backend can listen on port 443 without running as root.
* `smarterdog-record.service` uses ffmpeg to save the stream (video only, no re-encoding) as 10 minute `.mkv` files in `/srv/smarterdog`. It refuses to start unless `/srv/smarterdog` is a mount point, so recordings can never fill up the root filesystem.
* `smarterdog-cleanup.timer` deletes recordings older than 24 hours every hour.

```
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now smarterdog smarterdog-record smarterdog-cleanup.timer
```

Several RTSP clients (for example the recorder and VLC) can watch a camera at the same time. The camera is started for the first viewer, stopped when the last one leaves, and restarted automatically if its video stops.

## Browsing recordings

`viewer/` contains a small web app for browsing the recordings, with no dependencies beyond Python 3 and ffmpeg. It shows:

* A 24 hour activity strip with night hours (22:00–07:00) shaded. Click any point to play that moment.
* A card per recording, grouped by hour, with an animated thumbnail. When a recording contains motion, the thumbnail is made from the busiest moments.
* A player with a motion bar and a list of motion events to jump to. Recordings are converted to MP4 (without re-encoding) on demand so they play and seek in any browser.
* A sensitivity slider and "Motion only" / "Night only" filters.

A background indexer analyses each finished recording at 2 frames per second on a 64×36 grayscale copy, which takes a few seconds per 10 minute file. The top third of the picture is ignored, because it holds the camera's clock overlay and, from a floor level camera, mostly windows and ceiling where changing sunlight looks like motion. Overall brightness changes are compensated for, and a change across most of the picture at once (such as the camera switching to infrared) is treated as lighting rather than motion. Thumbnails and motion data are kept in `/srv/smarterdog/.index` and removed when their recording is deleted.

To install it, set `User=` in `systemd/smarterdog-viewer.service` to the user that owns `/srv/smarterdog`, then:

```
sudo cp systemd/smarterdog-viewer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now smarterdog-viewer
```

Then open `http://<yourhost>:8090/`. There is no login, so only expose it to your local network (with ufw, for example: `sudo ufw allow from 192.168.0.0/16 to any port 8090 proto tcp`).
