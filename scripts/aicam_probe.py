#!/usr/bin/env python3
"""
aicam_probe.py - find WHERE frames go missing (a "skip every second").

Measures frame timing at one point of the chain for N seconds and reports
gaps (missing frames) and how regularly they repeat. Run it on each stage,
from the camera towards OBS; the first stage that shows gaps is the culprit.

  1) the camera / test stream itself (decoded on the iGPU, like the core does):
       python aicam_probe.py rtsp://127.0.0.1:8554/cam1
       python aicam_probe.py "rtsp://admin:TaekwondoVAR@192.168.177.11:554"
  2) after the core pipeline, 1080p input of the AI (AI plugin running):
       python aicam_probe.py /tmp/aicam_raw_shm_socket --size 1920x1080
  3) the final AI picture that OBS reads:
       python aicam_probe.py /tmp/camera1_shm_socket

Shared-memory sockets can have several readers, so this can run next to OBS.
For RTSP it also checks the stream's own timestamps: if those already jump,
the camera / encoder (or ffmpeg test stream) is dropping frames, not us.
"""
import argparse
import statistics
import sys
import time

import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="rtsp://... URL or path of a shm socket")
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--size", default="1280x720", help="frame size of a shm socket (1920x1080 for the raw one)")
    ap.add_argument("--fps", type=int, default=30, help="nominal frame rate")
    ap.add_argument("--decoder", default="vah264dec", help="H.264 decoder for rtsp (avdec_h264 = CPU)")
    args = ap.parse_args()

    Gst.init(None)
    w, h = map(int, args.size.lower().split("x"))
    if args.target.startswith("rtsp://"):
        desc = (f"rtspsrc location={args.target} latency=800 ! rtph264depay ! h264parse ! "
                f"{args.decoder} ! fakesink name=sink sync=false async=false")
    else:
        desc = (f"shmsrc socket-path={args.target} is-live=true do-timestamp=true ! "
                f"video/x-raw,format=NV12,width={w},height={h},framerate={args.fps}/1 ! "
                f"fakesink name=sink sync=false async=false")
    pipe = Gst.parse_launch(desc)
    arrivals, pts = [], []

    def on_buffer(_pad, info):
        arrivals.append(time.monotonic())
        b = info.get_buffer()
        if b is not None and b.pts != Gst.CLOCK_TIME_NONE:
            pts.append(b.pts / 1e9)
        return Gst.PadProbeReturn.OK

    pipe.get_by_name("sink").get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, on_buffer)
    loop = GLib.MainLoop()
    bus = pipe.get_bus()
    bus.add_signal_watch()

    def on_msg(_b, m):
        if m.type == Gst.MessageType.ERROR:
            err, dbg = m.parse_error()
            print(f"ERROR: {err.message}\n{dbg}")
            loop.quit()

    bus.connect("message", on_msg)
    pipe.set_state(Gst.State.PLAYING)
    print(f"Measuring {args.target} for {args.seconds:.0f} s ...")
    GLib.timeout_add(int((args.seconds + 2) * 1000), loop.quit)     # +2 s warm-up
    loop.run()
    pipe.set_state(Gst.State.NULL)

    t = [x for x in arrivals if x - arrivals[0] >= 2.0] if arrivals else []
    if len(t) < 10:
        print("Too few frames received - is the source running / the path right?")
        return 1
    period = 1.0 / args.fps
    d = [b - a for a, b in zip(t, t[1:])]
    gaps = [(t[i + 1] - t[0], d[i]) for i in range(len(d)) if d[i] > 1.6 * period]
    dur = t[-1] - t[0]
    ds = sorted(d)
    print(f"\nframes   : {len(t)} in {dur:.1f} s  ->  {len(d) / dur:.2f} fps (nominal {args.fps})")
    print(f"interval : median {statistics.median(d) * 1000:.1f} ms   p99 {ds[int(len(ds) * 0.99)] * 1000:.1f} ms"
          f"   max {ds[-1] * 1000:.1f} ms")
    print(f"gaps     : {len(gaps)} (an interval longer than 1.6 frames = a frame missing or late)")
    if gaps:
        print("           at t = " + "  ".join(f"{g[0]:.2f}s({g[1] * 1000:.0f}ms)" for g in gaps[:12])
              + (" ..." if len(gaps) > 12 else ""))
        if len(gaps) >= 3:
            spacing = statistics.median(b[0] - a[0] for a, b in zip(gaps, gaps[1:]))
            print(f"           -> one every {spacing:.2f} s" + ("  (matches a 1 s keyframe interval)"
                                                            if 0.9 < spacing < 1.1 else ""))
    if args.target.startswith("rtsp://") and len(pts) > 10:
        p = sorted(set(pts))
        jumps = sum(1 for a, b in zip(p, p[1:]) if b - a > 1.6 * period)
        print(f"stream   : {jumps} jumps in the camera's own timestamps"
              + ("  -> the SOURCE drops frames (camera / encoder / ffmpeg)" if jumps else "  (source is complete)"))
    verdict = ("OK - no skips at this stage" if not gaps else
               "SKIPS at this stage - compare with the stage before it")
    print(f"\n{verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())