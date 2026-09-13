# `zoombot/bot/vendor/` — where the Zoom SDK archive goes, and does not live

Empty on purpose. The Zoom Meeting SDK for Linux is downloaded from the Zoom
App Marketplace under Zoom's own licence and is **not vendored in this
repository**.

Put the archive you downloaded here as:

```
zoombot/bot/vendor/zoom-meeting-sdk-linux.tar.xz
```

`entrypoint.sh` refuses to start without it, naming this file, rather than
booting an image that can join nothing while looking healthy. Do not commit
the archive.
