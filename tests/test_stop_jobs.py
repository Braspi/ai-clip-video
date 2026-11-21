"""A video can be stopped without losing ready clips, and deleted after its worker lets go."""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-stop-")
sys.path.insert(0, ".")

from studio import ctx, jobs, media  # noqa: E402


def video():
    return jobs.create({"type": "file", "path": "", "name": "video.mp4"}, {}, enqueue=False)


with ctx.as_user("owner_a"):
    queued = video()
    queued["clips"].append({"id": "clip01", "status": "done", "file": "clip01.mp4"})
    jobs.save(queued)
    jobs._q.put(("job", queued["id"], None), user="owner_a")
    jobs._q.put(("clip", queued["id"], "clip01"))
    assert jobs.stop(queued["id"])
    assert queued["status"] == "stopped" and queued["clips"][0]["status"] == "done"
    assert queued["id"] not in jobs._q.ahead()
    assert not any(item[0] == "clip" and item[1] == queued["id"] for item in jobs._q.quick)

    original_replace = Path.replace
    attempts = [0]

    def locked_once(path, target):
        attempts[0] += 1
        if attempts[0] <= 2:
            raise PermissionError("Windows is temporarily using the status file")
        return original_replace(path, target)

    with patch.object(Path, "replace", locked_once):
        jobs.save(queued)
    assert attempts[0] == 3 and (Path(queued["dir"]) / "job.json").exists()

    running = video()
    folder = Path(running["dir"])
    (folder / "ready.txt").write_text("test", encoding="utf-8")
    started = threading.Event()
    original = jobs._run_job

    def work(job):
        jobs._set(job, status="running", stage="Downloading")
        started.set()
        while not job.get("stop_requested"):
            time.sleep(0.02)
        raise jobs.JobStopped("Stopped by you")

    jobs._run_job = work
    jobs._q.put(("job", running["id"], None), user="owner_a")
    worker = threading.Thread(target=jobs._worker, daemon=True)
    worker.start()
    assert started.wait(3), "worker did not claim the video"
    assert jobs.stop(running["id"], delete_after=True)
    assert running["status"] == "stopping"
    for _ in range(100):
        if not folder.exists():
            break
        time.sleep(0.05)
    jobs._run_job = original
    assert not folder.exists() and jobs.get(running["id"]) is None

    ready = threading.Event()
    original_render = jobs.render_one
    def rerender_stopped(job, clip, words):
        clip["status"] = "done"
        jobs.save(job)
        ready.set()
    jobs.render_one = rerender_stopped
    queued["clips"][0].update(status="queued", captions=False)
    jobs.save(queued)
    jobs._q.put(("clip", queued["id"], "clip01"))
    assert ready.wait(3), "a stopped video's clip remake should run"
    jobs.render_one = original_render
    assert queued["clips"][0]["status"] == "done"

with ctx.as_user("other_b"):
    assert not jobs.stop(queued["id"]), "another user must not stop this video"
with ctx.as_user("owner_a"):
    assert queued["status"] == "stopped" and queued["clips"][0]["status"] == "done"

cancel = threading.Event()
timer = threading.Timer(0.2, cancel.set)
timer.start()
began = time.monotonic()
try:
    media.run([sys.executable, "-c", "import time; time.sleep(20)"], cancel=cancel.is_set, timeout=5)
    assert False, "the external process kept running"
except InterruptedError:
    assert time.monotonic() - began < 3
finally:
    timer.cancel()

print("PASS queued work stops without losing ready clips")
print("PASS a temporary Windows file lock does not fail a status save")
print("PASS running work deletes only after its worker releases the files")
print("PASS a finished clip can be remade after its video is stopped")
print("PASS another user cannot stop the video")
print("PASS a running media process stops promptly")
