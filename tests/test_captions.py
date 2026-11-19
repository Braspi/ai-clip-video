"""Check caption preferences and the subtitle events sent to the encoder."""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np

from fastapi import HTTPException

sys.path.insert(0, ".")
WORDS = [{"w": "spoken", "s": 1.0, "e": 1.5}]
FIXED = [{"w": "polished", "s": 1.0, "e": 1.5}]


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    print(f"PASS {name}")


def has_dialogue(ass, style, text=""):
    return any(line.startswith("Dialogue: ") and f",{style}," in line and text in line
               for line in ass.splitlines())


def rejects_caption_value(call):
    try:
        call()
    except HTTPException as error:
        return error.status_code == 400 and "captions" in str(error.detail).lower()
    return False


def main(data):
    os.environ.update(PEAKCUT_DATA=data, DATABASE_URL="", PEAKCUT_ACCOUNTS="0")
    from studio import auth, brain, creators, ctx, db, jobs, render, roman, server

    db.init()
    try:
        user = auth.local_user().id
        source = {"type": "file", "name": "Caption test"}
        clip = {"id": "clip1", "start": 0.0, "end": 4.0, "status": "done", "hook": "KEEP THE HOOK",
                "title": "A clip", "description": "", "hashtags": [], "score": 95}

        with ctx.as_user(user):
            creator = creators.create({"name": "Caption test creator", "captions": False})
            inherited = jobs.create(source, {}, enqueue=False, creator=creator["id"])
            live_job = jobs.create(source, {}, enqueue=False, creator=creator["id"], kind="live")
            override = jobs.create(source, {"captions": True}, enqueue=False, creator=creator["id"])
            check("videos and live jobs inherit the creator's caption preference",
                  inherited["options"]["captions"] is False and live_job["options"]["captions"] is False)
            check("a job can override its creator's caption preference", override["options"]["captions"] is True)
            styled = jobs.create(source, {"style": "neon", "layout": "zoom", "max_clips": 4,
                                          "min_len": 20, "max_len": 45, "quality": 1080,
                                          "range_start": 60, "range_end": 300}, enqueue=False)
            check("the chosen video options are stored on the new job",
                  all(styled["options"][k] == v for k, v in {
                      "style": "neon", "layout": "zoom", "max_clips": 4, "min_len": 20,
                      "max_len": 45, "quality": 1080, "range_start": 60, "range_end": 300}.items()))
            for name in ("minimal", "neon", "sunset", "focus"):
                style = render.STYLES[name]
                ass = render.build_ass(WORDS, 0, 4, "", name, [], 300, 1400)
                check(f"the {name} style renders with its own font and colour",
                      f"Style: Cap,{style['font']},{style['size']}" in ass and
                      f"\\c&H{style['active']}&" in ass)
            minimal = render.build_ass(WORDS, 0, 4, "", "minimal", [], 300, 1400)
            check("the minimal style has no pop animation", "\\fscx94" not in minimal)
            for quality, expected in ((720, "scale=720:1280:flags=lanczos"), (1080, "ass=sample.ass:fontsdir=f[v]")):
                work = Path(data) / f"render-{quality}"
                work.mkdir()
                with (patch.object(render.subprocess, "run", return_value=type("Done", (), {"returncode": 0})()) as ffmpeg,
                      patch.object(render, "run"), patch.object(render.shutil, "move")):
                    render._render_in(work, Path(data) / "source.mp4", Path(data), "sample", 0, 4,
                                      "crop", 1920, minimal, quality=quality)
                    cmd = ffmpeg.call_args.args[0]
                    filter_graph = cmd[cmd.index("-filter_complex") + 1]
                    check(f"the {quality}p selection reaches the video export", expected in filter_graph)

            job = jobs.create(source, {}, enqueue=False)
            check("the default is to show captions", job["options"]["captions"] is True)
            job["options"].pop("captions")
            job.update(status="done", lang="ar", source_path=str(Path(data) / "source.mp4"), width=1920, height=1080,
                       duration=4.0, clips=[clip])
            jobs.save(job)

        with (ctx.as_user(user), patch.object(render, "_render_in", return_value={"file": "clip1.mp4"}) as encode,
              patch.object(brain, "english_captions", return_value=FIXED) as prepare,
              patch.object(roman, "words", wraps=roman.words) as romanize,
              patch.object(jobs._q, "put") as queue):
            jobs.render_one(job, clip, WORDS)
            ass = encode.call_args.args[-1]
            check("an older saved clip still renders spoken captions",
                  clip["status"] == "done" and has_dialogue(ass, "Cap", "POLISHED"))
            check("the job's output quality is sent to the renderer", encode.call_args.kwargs["quality"] == 720)
            check("caption preparation is cached for the clip", prepare.call_count == 1 and clip["fixed_words"] == FIXED)

            for value in ("false", 0, None):
                check(f"the clip editor and creator settings reject {value!r} as a caption preference",
                      rejects_caption_value(lambda: server.rerender(job["id"], clip["id"], {"captions": value}))
                      and rejects_caption_value(lambda: server.new_creator({"name": "Invalid", "captions": value})))
            check("invalid caption changes do not queue a render", not queue.called)

            prepare.reset_mock()
            romanize.reset_mock()
            response = server.rerender(job["id"], clip["id"], {"captions": False})
            check("the clip editor can turn captions off", response["clips"][0]["captions"] is False)
            check("turning captions off queues the clip for rendering", queue.call_args.args[0] == ("clip", job["id"], clip["id"]))
            jobs.render_one(job, clip, WORDS)
            ass = encode.call_args.args[-1]
            check("turning off captions keeps the hook and skips caption processing",
                  clip["status"] == "done" and not has_dialogue(ass, "Cap") and has_dialogue(ass, "Hook", "KEEP THE HOOK")
                  and not prepare.called and not romanize.called)
            saved = json.loads((Path(job["dir"]) / "job.json").read_text(encoding="utf-8"))["clips"][0]
            check("the saved clip retains both the preference and the cached words",
                  saved["captions"] is False and saved["fixed_words"] == FIXED)

            response = server.rerender(job["id"], clip["id"], {"captions": True})
            check("the clip editor can turn captions back on", response["clips"][0]["captions"] is True)
            jobs.render_one(job, clip, WORDS)
            check("turning captions back on uses the cached words",
                  clip["status"] == "done" and has_dialogue(encode.call_args.args[-1], "Cap", "POLISHED")
                  and not prepare.called)

            rows = server.clip_captions(job["id"], clip["id"])
            check("the editor returns this clip's timed caption lines",
                  rows["rows"] and rows["rows"][0]["text"] == "polished")
            response = server.rerender(job["id"], clip["id"], {
                "manual_captions": [{"s": 1.0, "e": 1.5, "text": "Dokładnie dobrze"}]})
            check("manual caption words stay private in public job data",
                  "caption_override_words" not in response["clips"][0])
            jobs.render_one(job, clip, WORDS)
            check("manual corrections are rendered without AI rewriting them",
                  has_dialogue(encode.call_args.args[-1], "Cap", "DOKŁADNIE") and
                  has_dialogue(encode.call_args.args[-1], "Cap", "DOBRZE") and not prepare.called)
            check("the editor shows the corrected words",
                  server.clip_captions(job["id"], clip["id"])["rows"][0]["text"] == "Dokładnie dobrze")

            def recognize(audio, language=None, language_out=None):
                check("only the chosen clip is sent to local speech recognition", len(audio) == 16000 and language == "en")
                language_out["language"] = "en"
                return [{"w": "actually", "s": 0.25, "e": 0.75}]

            with (patch.object(jobs.media, "run") as extract,
                  patch.object(jobs.media, "load_audio", return_value=np.ones(16000, dtype=np.int16)),
                  patch.object(jobs.transcribe, "transcribe", side_effect=recognize)):
                server.rerender(job["id"], clip["id"], {"caption_language": "en", "retranscribe": True})
                jobs.render_one(job, clip, WORDS)
                check("recognition extracts only this clip's audio", "-t" in extract.call_args.args[0] and
                      "4.00" in extract.call_args.args[0])
            check("recognized words replace the wrong-language transcript",
                  clip["status"] == "done" and clip["caption_detected_language"] == "en" and
                  has_dialogue(encode.call_args.args[-1], "Cap", "ACTUALLY"))
            check("the selected language belongs only to the clip", job.get("lang") == "ar" and
                  clip["caption_language"] == "en")

            try:
                server.rerender(job["id"], clip["id"], {"caption_language": "not-a-language", "retranscribe": True})
            except HTTPException as error:
                check("unsupported languages are rejected", error.status_code == 400)
            else:
                raise AssertionError("unsupported languages are rejected")

            render.render_clip(Path(data) / "source.mp4", Path(data) / "clips", "direct",
                               dict(clip, captions=False), WORDS, 1920, 1080)
            ass = encode.call_args.args[-1]
            check("the renderer honors captions off even when it receives spoken words",
                  not has_dialogue(ass, "Cap") and has_dialogue(ass, "Hook", "KEEP THE HOOK"))
    finally:
        db.engine.dispose()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="pc-captions-") as data:
        main(data)
