"""Post queue: decides when each finished clip goes to which account, posts it, and tracks views."""
import secrets
import threading
import time
from pathlib import Path

import json
import math

from . import accounts, alerts, config, creators, ctx, jobs, live

_lock = threading.RLock()
_FILE = config.DATA_DIR / "posts.json"
try:
    _posts: list = json.loads(_FILE.read_text(encoding="utf-8")) if _FILE.exists() else []
except ValueError:
    _posts = []


def _mine() -> list:
    u = ctx.uid()
    return [p for p in _posts if p.get("user") == u]
OPEN = ("scheduled", "posting", "yt_scheduled")


def _save():
    with _lock:
        tmp = _FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_posts, indent=1), encoding="utf-8")
        tmp.replace(_FILE)


def forget(uid: str):
    """Drop everything queued or recorded for a deleted account, so nothing is posted or retried for it any more."""
    with _lock:
        for p in _posts:
            if p.get("user") == uid:
                p["status"] = "deleted"          # the posting loop may be holding this one already: it then skips it
        _posts[:] = [p for p in _posts if p.get("user") != uid]
        _save()


def _day(t):
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _mirror(p):
    """Copy the post's state onto its clip so the clip card can show it."""
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    if clip is not None:
        clip.setdefault("posts", {})[p["id"]] = {k: p.get(k) for k in ("account_name", "platform", "status", "url", "due_at", "error")}
        jobs.save(job)


def _next_slot(account_id, gap_min, cap):
    """Earliest time that respects the gap between posts and the daily cap for this account."""
    mine = [p for p in _mine() if p["account"] == account_id and p["status"] in OPEN + ("posted",)]
    t = max(time.time(), max((p.get("posted_at") or p["due_at"] for p in mine), default=0) + gap_min * 60)
    for _ in range(30):
        if sum(1 for p in mine if _day(p.get("posted_at") or p["due_at"]) == _day(t)) < cap:
            break
        lt = time.localtime(t + 86400)
        t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 9, 0, 0, 0, 0, -1))  # 9:00 next day
    return t


def _when(value) -> float:
    try:
        due = float(value)
    except (TypeError, ValueError):
        raise ValueError("Choose a valid date and time.")
    if not math.isfinite(due) or due < time.time() + 30:
        raise ValueError("Choose a time at least 30 seconds from now.")
    if due > time.time() + 366 * 86400:
        raise ValueError("Posts can be scheduled up to one year ahead.")
    return due


def enqueue(job, clip, account_ids, now=False, due_at=None, gap_min=None, daily_cap=None) -> list:
    c = creators.get(job["creator"]) or creators.DEFAULTS
    made = []
    with _lock:
        for aid in account_ids:
            acc = accounts.get(aid)
            if not acc:
                continue
            if any(p["job"] == job["id"] and p["clip"] == clip["id"] and p["account"] == aid
                   and p["status"] in OPEN + ("posted",) for p in _mine()):
                continue  # never post the same clip to the same account twice
            due = time.time() if now else (_when(due_at) if due_at is not None else
                                            _next_slot(aid, gap_min or c["post_gap_min"], daily_cap or c["daily_cap"]))
            p = {"id": secrets.token_hex(8), "user": ctx.uid(), "job": job["id"], "clip": clip["id"], "creator": job["creator"],
                 "account": aid, "account_name": acc["name"], "platform": acc["platform"], "status": "scheduled",
                 "due_at": due, "created": time.time(), "title": clip.get("title") or clip.get("hook") or "Clip",
                 "score": clip.get("score", 0), "tries": 0, "url": "", "remote_id": "", "error": None, "views": None}
            _posts.append(p)
            made.append(p)
        _save()
    for p in made:
        _mirror(p)
    return made


def enqueue_batch(job, clips, account_id, per_day=3) -> list:
    """Queue one channel's best clips in local-time slots, capped at 3 or 4 a day."""
    acc = accounts.get(account_id)
    if not acc or acc.get("platform") != "youtube":
        raise ValueError("Choose a connected YouTube channel.")
    per_day = 4 if int(per_day) == 4 else 3
    aid = acc["id"]
    with _lock:
        existing = [p for p in _mine() if p.get("account") == aid
                    and p.get("status") in OPEN + ("posted",)]
    due_values = [float(p.get("posted_at") or p.get("due_at") or 0) for p in existing]
    due_values = [v for v in due_values if v > 0]
    day_counts = {}
    for p, due in zip(existing, [float(p.get("posted_at") or p.get("due_at") or 0) for p in existing]):
        if due:
            day_counts[_day(due)] = day_counts.get(_day(due), 0) + 1
    hours = (9, 12, 15, 18) if per_day == 4 else (11, 15, 19)
    cursor = time.time() + 10 * 60
    slots = []
    # Leave at least an hour between posts already planned for this channel.
    while len(slots) < len(clips):
        lt = time.localtime(cursor)
        date = (lt.tm_year, lt.tm_mon, lt.tm_mday)
        key = time.strftime("%Y-%m-%d", lt)
        if day_counts.get(key, 0) < per_day:
            for hour in hours:
                candidate = time.mktime((date[0], date[1], date[2], hour, 0, 0, 0, 0, -1))
                if candidate < cursor:
                    continue
                if any(abs(candidate - t) < 3600 for t in due_values + slots):
                    continue
                slots.append(candidate)
                if day_counts.get(key, 0) + sum(_day(t) == key for t in slots) >= per_day:
                    break
                if len(slots) >= len(clips):
                    break
        next_day = time.localtime(time.mktime((date[0], date[1], date[2] + 1, 0, 0, 0, 0, 0, -1)))
        cursor = time.mktime((next_day.tm_year, next_day.tm_mon, next_day.tm_mday, 0, 0, 0, 0, 0, -1))
        if cursor > time.time() + 366 * 86400:
            raise ValueError("There are too many clips to schedule within the next year.")
    made = []
    for clip, due in zip(clips, slots):
        made.extend(enqueue(job, clip, [aid], due_at=due))
    return made


def already_queued(job_id, clip_id, account_id) -> bool:
    return any(p["job"] == job_id and p["clip"] == clip_id and p["account"] == account_id
               and p["status"] in OPEN + ("posted",) for p in _mine())


def on_clip_ready(job, clip):
    """Called once when a clip first finishes rendering."""
    c = creators.get(job["creator"])
    if not c:
        return
    if not c.get("source_rights_confirmed"):
        if c.get("autopost") or (job.get("type") == "live" and c.get("auto_watch") and c.get("live_autopost")):
            creators.log(c["id"], "Automatic posting paused: confirm you have permission to publish clips from this source in Setup.")
        return
    if job.get("type") == "live" and c.get("auto_watch") and c.get("live_autopost"):
        # Live monitoring gets its own opt-in and only publishes to YouTube. The regular autopost
        # switch remains responsible for clips from uploaded VODs and other selected platforms.
        if clip.get("score", 0) < 80:
            creators.log(c["id"], f"Live clip not queued: score {clip.get('score', 0)}/100 is below 80")
            return
        youtube_ids = [aid for aid in c["accounts"] if (accounts.get(aid) or {}).get("platform") == "youtube"]
        if not youtube_ids:
            return
        made = enqueue(job, clip, youtube_ids, gap_min=max(30, c.get("post_gap_min", 30)),
                       daily_cap=min(4, max(3, c.get("daily_cap", 4))))
    elif c["autopost"] and c["accounts"]:
        if clip.get("score", 0) < c["post_min_score"]:
            return  # includes hand-made clips (score 0): those wait for you to press Post
        made = enqueue(job, clip, c["accounts"])
    else:
        return
    if made:
        creators.log(c["id"], f"Queued for {len(made)} account(s): {clip.get('title') or clip.get('hook')}")


def _caption(clip, creator):
    tags = " ".join("#" + h for h in clip.get("hashtags", []))
    return "\n\n".join(x for x in (clip.get("description", ""), tags, creator.get("post_footer", "")) if x)


def _post(p):
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    acc = accounts.get(p["account"])
    try:
        if not clip or clip.get("status") != "done":
            raise RuntimeError("The clip no longer exists or is not finished.")
        if not acc:
            raise RuntimeError("That account was disconnected.")
        path = Path(job["dir"]) / "clips" / clip["file"]
        creator = creators.get(job["creator"]) or dict(creators.DEFAULTS, name="clips")
        title, cap = clip.get("title") or clip.get("hook") or "Clip", _caption(clip, creator)
        if acc["platform"] == "youtube":
            native_time = p["due_at"] if p["due_at"] > time.time() + 300 else None
            if p.get("remote_id"):
                accounts.youtube_change_schedule(acc, p["remote_id"], native_time, privacy="private" if native_time else "public")
                rid, url = p["remote_id"], p.get("url") or f"https://www.youtube.com/shorts/{p['remote_id']}"
            else:
                rid, url = accounts.youtube_upload(acc, path, title, cap + "\n\n#Shorts", clip.get("hashtags", []), creator["privacy"], publish_at=native_time)
            if native_time:
                p.update(status="yt_scheduled", uploaded_at=p.get("uploaded_at") or time.time(), remote_id=rid, url=url, error=None)
                creators.log(p["creator"], f"Uploaded to YouTube and scheduled: {title}")
            else:
                p.update(status="posted", posted_at=time.time(), remote_id=rid, url=url, error=None)
                creators.log(p["creator"], f"Posted to {acc['name']}: {title}")
        elif acc["platform"] == "instagram":
            rid, url = accounts.instagram_upload(acc, path, f"{title}\n\n{cap}", job["id"])
            p.update(status="posted", posted_at=time.time(), remote_id=rid, url=url, error=None)
            creators.log(p["creator"], f"Posted to {acc['name']}: {title}")
        elif acc["platform"] == "tiktok":
            rid, url = accounts.tiktok_upload(acc, path, f"{title}\n\n{cap}", creator["privacy"])
            p.update(status="posted", posted_at=time.time(), remote_id=rid, url=url, error=None)
            creators.log(p["creator"], f"Posted to {acc['name']}: {title}")
        else:
            raise RuntimeError("That kind of account is no longer supported.")
    except Exception as e:  # noqa: BLE001
        p["tries"] += 1
        p["error"] = str(e)[-300:]
        if p["tries"] < 3 and clip and acc:
            p.update(status="scheduled", due_at=time.time() + 600)  # try again in 10 minutes
        else:
            p["status"] = "failed"
            creators.log(p["creator"], f"Post failed on {p['account_name']}: {p['error']}")
            alerts.notify(f"post failed on {p['account_name']} ({p['title']}): {p['error'][:200]}", kind="post")
    _save()
    _mirror(p)


def _run():
    while True:
        time.sleep(15)
        now = time.time()
        published = [p for p in list(_posts) if p["status"] == "yt_scheduled" and p["due_at"] <= now and p.get("user")]
        for p in published:
            with _lock:
                if p["status"] == "yt_scheduled":
                    p.update(status="posted", posted_at=p["due_at"])
                    _save()
            with ctx.as_user(p["user"]):
                _mirror(p)
        due = [p for p in list(_posts) if p["status"] == "scheduled" and p.get("user") and
               (p["due_at"] <= now or (p["platform"] == "youtube" and p["due_at"] > now + 300))]
        for p in due:
            try:
                with ctx.as_user(p["user"]):
                    if accounts.settings()["paused"]:
                        continue
                    with _lock:
                        if p["status"] != "scheduled":
                            continue
                        p["status"] = "posting"
                    _post(p)
            except Exception:  # noqa: BLE001
                pass


def _stats_for(recent):
    for aid in {p["account"] for p in recent if p["platform"] == "youtube"}:
        acc = accounts.get(aid)
        if acc:
            views = accounts.youtube_views(acc, [p["remote_id"] for p in recent if p["account"] == aid])
            for p in recent:
                if p["account"] == aid and p["remote_id"] in views:
                    p["views"] = views[p["remote_id"]]
    for p in recent:
        acc = accounts.get(p["account"]) if p["platform"] == "instagram" else None
        if acc:
            v = accounts.instagram_views(acc, p["remote_id"])
            if v is not None:
                p["views"] = v


def _stats():
    while True:
        for uid in {p.get("user") for p in _posts if p.get("user")}:
            try:
                with ctx.as_user(uid):
                    _stats_for([p for p in _mine() if p["status"] == "posted" and time.time() - p["posted_at"] < 14 * 86400])
            except Exception:  # noqa: BLE001
                pass
        _save()
        time.sleep(1800)


def cancel(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] not in ("scheduled", "yt_scheduled"):
            raise ValueError("Only posts that are still waiting can be cancelled.")
    if p.get("platform") == "youtube" and p.get("remote_id"):
        acc = accounts.get(p["account"])
        if not acc:
            raise ValueError("That YouTube account was disconnected.")
        accounts.youtube_change_schedule(acc, p["remote_id"])
    with _lock:
        if p["status"] not in ("scheduled", "yt_scheduled"):
            raise ValueError("This post has already started publishing.")
        p["status"] = "cancelled"
        _save()
    _mirror(p)


def retry(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] not in ("failed", "cancelled"):
            raise ValueError("Only failed or cancelled posts can be retried.")
        if p.get("platform") == "youtube" and p.get("remote_id"):
            acc = accounts.get(p["account"])
            if not acc:
                raise ValueError("That YouTube account was disconnected.")
            due = time.time() + 300
        else:
            acc, due = None, time.time()
    if acc:
        accounts.youtube_change_schedule(acc, p["remote_id"], due)
    with _lock:
        p.update(status="yt_scheduled" if acc else "scheduled", due_at=due, tries=0, error=None)
        _save()
    _mirror(p)


def reschedule(pid, due_at):
    due = _when(due_at)
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] not in ("scheduled", "yt_scheduled"):
            raise ValueError("Only posts that are still waiting can be rescheduled.")
        remote_youtube = p.get("platform") == "youtube" and bool(p.get("remote_id"))
    yt_scheduled = remote_youtube and due > time.time() + 300
    if remote_youtube:
        acc = accounts.get(p["account"])
        if not acc:
            raise ValueError("That YouTube account was disconnected.")
        accounts.youtube_change_schedule(acc, p["remote_id"], due if yt_scheduled else None)
    with _lock:
        if p["status"] not in ("scheduled", "yt_scheduled"):
            raise ValueError("This post has already started publishing.")
        p.update(due_at=due, status="yt_scheduled" if yt_scheduled else "scheduled", error=None)
        _save()
    _mirror(p)
    return p


def sync_clip_text(job_id, clip_id, title):
    """Keep a waiting calendar card's title in step with its editable clip."""
    changed = False
    with _lock:
        for p in _mine():
            if p["job"] == job_id and p["clip"] == clip_id and p["status"] in OPEN:
                p["title"] = title
                changed = True
        if changed:
            _save()


def overview() -> dict:
    today = _day(time.time())
    _posts = _mine()  # everything below is about the signed-in user only
    rows = []
    all_cs, all_js = creators.all_creators(), jobs.list_jobs()
    for c in all_cs:
        js = [j for j in all_js if j["creator"] == c["id"]]
        clips = [k for j in js for k in j["clips"] if k.get("status") == "done"]
        ps = [p for p in _posts if p["creator"] == c["id"]]
        views = sum(p["views"] or 0 for p in ps)
        rows.append({
            "id": c["id"], "name": c["name"],
            "state": "recording" if live.active_for(c["id"]) else "waiting" if c["auto_watch"] and c["channel"] else "idle",
            "autopost": c["autopost"] and bool(c["accounts"]),
            "clips_today": sum(1 for k in clips if _day(k.get("made_at", 0)) == today), "clips_total": len(clips),
            "posted_today": sum(1 for p in ps if p["status"] == "posted" and _day(p["posted_at"]) == today),
            "posted_total": sum(1 for p in ps if p["status"] == "posted"),
            "queued": sum(1 for p in ps if p["status"] in OPEN), "failed": sum(1 for p in ps if p["status"] == "failed"),
            "views": views, "earnings": round(views / 1000 * c["rate_per_1k"], 2)})
    names = {c["id"]: c["name"] for c in all_cs}
    queue = sorted((p for p in _posts if p["status"] in OPEN), key=lambda p: p["due_at"])
    done = sorted((p for p in _posts if p["status"] in ("posted", "failed")), key=lambda p: -(p.get("posted_at") or p["due_at"]))[:15]
    show = [{**{k: v for k, v in p.items() if k != "user"}, "creator_name": names.get(p["creator"], "")} for p in queue + done]
    # last 14 days, oldest first, for the home graph
    days = [_day(time.time() - 86400 * i) for i in range(13, -1, -1)]
    made, posted, views = {d: 0 for d in days}, {d: 0 for d in days}, {d: 0 for d in days}
    for j in all_js:
        for k in j["clips"]:
            d = _day(k.get("made_at") or j["created"])
            if k.get("status") == "done" and d in made:
                made[d] += 1
    for p in _posts:
        if p["status"] == "posted" and _day(p["posted_at"]) in posted:
            posted[_day(p["posted_at"])] += 1
            views[_day(p["posted_at"])] += p["views"] or 0
    daily = [{"day": d, "clips": made[d], "posted": posted[d], "views": views[d]} for d in days]
    return {"paused": accounts.settings()["paused"], "creators": rows, "posts": show, "daily": daily,
            "alerts": alerts.configured(), "recording": len(live.active_mine())}


def digest_text() -> str:
    o = overview()
    lines = ["PeakCut daily summary"]
    for r in o["creators"]:
        lines.append(f"- {r['name']}: {r['state']}, {r['clips_today']} clips today, {r['posted_today']} posted today, "
                     f"{r['queued']} waiting, {r['failed']} failed, {r['views']} views")
    if not o["creators"]:
        lines.append("No creators set up yet.")
    if o["paused"]:
        lines.append("Posting is paused.")
    return "\n".join(lines)


def start():
    with _lock:
        for p in _posts:
            if p["status"] == "posting":  # interrupted mid-upload last time
                p.update(status="failed", error="The studio closed during this upload. Check the account, then retry.")
        _save()
    jobs.on_clip_ready.append(on_clip_ready)
    threading.Thread(target=_run, daemon=True).start()
    threading.Thread(target=_stats, daemon=True).start()
    threading.Thread(target=alerts.digest_loop, args=(digest_text,), daemon=True).start()
    threading.Thread(target=alerts.keep_awake_loop,
                     args=(lambda: bool(live.active()) or any(p["status"] in OPEN for p in _posts),), daemon=True).start()
