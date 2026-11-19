"""Live decisions stay bounded and keep strong moments when the local model times out."""
import copy
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-live-decision-")
sys.path.insert(0, ".")
from studio import brain, creators, live, posting


def words(start=0, count=45):
    return [{"w": f"word{i}", "s": float(start + i), "e": float(start + i) + 0.6} for i in range(count)]


def test_short_prompt():
    seen = []

    def answer(prompt, **kwargs):
        seen.append((prompt, kwargs))
        return [], "model", 10

    with patch.object(brain, "ask_ai", answer):
        result, tokens = brain.judge_live(words(count=200), 0, 200, signals="[140] L=+2.4 C=5", timeout=12)
    assert result == [] and tokens == 10
    assert len(seen) == 1 and seen[0][1]["max_output_tokens"] == 450
    assert "word199" in seen[0][0]
    assert len(seen[0][0]) < 6500


def session(root):
    settings = copy.deepcopy(creators.DEFAULTS)
    settings.update(min_len=15, max_len=60, ai_every_min=2, min_score=65, min_gap_min=0)
    job = {"dir": str(root), "creator": "test", "title": "Test stream", "uploader": "Streamer",
           "clips": [], "ai": {}, "options": {"captions": True}}
    s = live.Session(job, settings)
    s.words = words(50, 46)
    s.t_done = 100.0
    s.db = [-40.0] * 101
    s.db[76] = -5.0
    s.chat.update({76: 20})
    s.chat_on = True
    s._game = lambda: ""
    return s


def test_one_call_and_review_fallback():
    with tempfile.TemporaryDirectory() as folder:
        s = session(Path(folder))
        added, called = [], []
        s._add_clip = lambda c: added.append(c)

        def timeout(*args, **kwargs):
            called.append(1)
            raise TimeoutError("Local model timed out")

        with patch.object(brain, "judge_live", timeout):
            s._ai_check()
        assert len(called) == 1, "a live check should never make a second model request"
        assert len(added) == 1, "a strong spoken reaction should survive an AI timeout"
        assert added[0]["needs_review"] and added[0]["score"] < 80
        assert s.verdicts[-1]["result"].startswith("Clipped:")


def test_review_clip_cannot_autopost():
    creator = copy.deepcopy(creators.DEFAULTS)
    creator.update(source_rights_confirmed=True, auto_watch=True, live_autopost=True, accounts=["youtube"])
    with patch.object(creators, "get", return_value=creator), patch.object(posting, "enqueue") as enqueue:
        posting.on_clip_ready({"type": "live", "creator": "test"}, {"score": 100, "needs_review": True})
    enqueue.assert_not_called()


def test_recording_gap():
    complete = [("one.ts", 0, 12), ("two.ts", 12, 24), ("three.ts", 24, 36)]
    interrupted = [("one.ts", 0, 12), ("three.ts", 24, 36)]
    assert live._video_gap(complete, 5, 30) == 0
    assert live._video_gap(interrupted, 5, 30) == 12


if __name__ == "__main__":
    test_short_prompt()
    test_one_call_and_review_fallback()
    test_review_clip_cannot_autopost()
    test_recording_gap()
    print("PASS quick live decision")
    print("PASS timeout preserves a reviewable clip")
    print("PASS review clip cannot post automatically")
    print("PASS missing video is detected before rendering")
