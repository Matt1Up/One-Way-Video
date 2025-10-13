#!/bin/bash
/usr/sbin/modprobe -r v4l2loopback || true
/usr/sbin/modprobe v4l2loopback devices=1 video_nr=2 card_label="OBS-Virtual" exclusive_caps=1
sleep 1
/usr/bin/setfacl -m u:matt1up:rw /dev/video2
/usr/bin/ls -l /dev/video2
/usr/bin/getfacl /dev/video2
