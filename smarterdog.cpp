/*
This file is part of SmarterDog (C) 2017 Erik de Jong

SmarterDog is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

SmarterDog is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with SmarterDog. If not, see <https://www.gnu.org/licenses/>
*/

#include "smarterdog.h"

SmarterDog::SmarterDog(QObject *parent) : QObject(parent)
{
	qout = new QTextStream(stdout);
	cleverdogHandler = new CleverdogUDP(parent);
	connect(cleverdogHandler, SIGNAL(scanResult(QHostAddress,QString,QString,QString)), this, SLOT(scanResult(QHostAddress,QString,QString,QString)));
	connect(cleverdogHandler, SIGNAL(log(QString)), this, SLOT(log(QString)));
	cleverdogBackend = new CleverdogBackend();
    scanTime = 1000; // end scans after 1 second
	connect(&streamWatchdog, SIGNAL(timeout()), this, SLOT(checkStreams()));
}

void SmarterDog::scan()
{
	cleverdogHandler->scanForDevices();
	QTimer::singleShot(scanTime, this, SLOT(terminate()));
	*qout << "Scan results:" << endl;
}

void SmarterDog::stream(QStringList cidList)
{
	rtspServer = new RtspHandler(8086, this);
	// Fetch current address for selected cameras
	foreach (QString cid, cidList) {
		CleverdogCamera *camera = new CleverdogCamera();
		cameras.insert(cid, camera);
	}
	cleverdogHandler->scanForDevices();
	QTimer::singleShot(scanTime, this, SLOT(scanTimeout()));
	streamWatchdog.start(5000);
}

uint16_t SmarterDog::getRtpSourcePort(QString CID)
{
	if (cameras.contains(CID)) {
		return cameras.value(CID)->recaster->getRtpPort();
	}
	return 0;
}

void SmarterDog::log(QString message)
{
	*qout << message << endl;
}

void SmarterDog::scanResult(QHostAddress host, QString CID, QString mac, QString firmware)
{
	if (CID != "" && mac != "" && firmware != "") {
		CleverdogCamera *camera = new CleverdogCamera(host, CID, mac, firmware);
		if (cameras.contains(CID)) {
			delete cameras.value(CID);
		}
		cameras.insert(CID, camera);
		log(QString("%1 %2 %3 %4").arg(host.toString(), CID, mac, firmware));
	}
}

void SmarterDog::startStream(QString CID, QHostAddress destinationHost, uint16_t destionationPort)
{
	if (cameras.contains(CID)) {
		CleverdogCamera *camera = cameras.value(CID);
		bool wasStreaming = camera->recaster->destinationCount() > 0;
		camera->recaster->addRtpDestination(destinationHost, destionationPort);
		qint64 idle = camera->recaster->msSinceLastPacket();
		/* Only (re)start the camera if nobody was watching or the stream has stalled */
		if (!wasStreaming || idle < 0 || idle > 3000) {
			cleverdogHandler->startRtp(camera->address, camera->cid, camera->recaster->getRtpPort());
		}
		log(QString("Viewer %1:%2 added to %3, %4 viewer(s)").arg(destinationHost.toString()).arg(destionationPort).arg(CID).arg(camera->recaster->destinationCount()));
	}
}

void SmarterDog::stopStream(QString CID, QHostAddress destinationHost, uint16_t destionationPort)
{
	if (cameras.contains(CID)) {
		CleverdogCamera *camera = cameras.value(CID);
		camera->recaster->removeRtpDestination(destinationHost, destionationPort);
		/* Only stop the camera once the last viewer has gone */
		if (camera->recaster->destinationCount() == 0) {
			cleverdogHandler->stopRtp(camera->address, camera->cid);
		}
		log(QString("Viewer %1:%2 removed from %3, %4 viewer(s)").arg(destinationHost.toString()).arg(destionationPort).arg(CID).arg(camera->recaster->destinationCount()));
	}
}

void SmarterDog::terminate()
{
	emit finished();
}

void SmarterDog::checkStreams()
{
	/* Restart cameras that have viewers but stopped sending video */
	foreach (CleverdogCamera *camera, cameras) {
		if (camera->address.isNull() || camera->recaster->destinationCount() == 0) {
			continue;
		}
		qint64 idle = camera->recaster->msSinceLastPacket();
		if (idle < 0 || idle > 5000) {
			log("No video from " + camera->cid + ", restarting stream");
			cleverdogHandler->startRtp(camera->address, camera->cid, camera->recaster->getRtpPort());
		}
	}
}

void SmarterDog::scanTimeout()
{
	foreach (CleverdogCamera *camera, cameras) {
		if (camera->address.isNull()) {
			log("Camera " + cameras.key(camera) + " not detected");
		}
	}
}
