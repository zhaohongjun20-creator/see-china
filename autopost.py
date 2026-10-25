# -*- coding: utf-8 -*-
"""《带你看中国 See China》——面向外国观众的英文中国介绍系列。

GLM 英文讲稿 → Wikimedia 中国主题配图 → 英文配音 → Ken Burns 成片（含英文字幕）
→ output/ 下产出 final.mp4 + upload_info.txt（标题/描述/标签，手动上传 YouTube/Instagram 用）。
"""
import json, logging, os, random, re, sys
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from src import script as script_mod
from src import narrator
from src import slideshow
from src.store import Store

LOG_DIR, IMG_DIR, AUD_DIR, DL_DIR, DATA_DIR = "logs", "imgs", "audio", "downloads", "data"
SERIES = "See China"


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(f"{LOG_DIR}/{datetime.now():%Y-%m}.log", encoding="utf-8"),
                  logging.StreamHandler()])


def load_config():
    import yaml
    with open("config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def run(topic_index: int | None):
    log = logging.getLogger("main")
    load_dotenv()
    if not os.environ.get('PEXELS_API_KEY'):
        load_dotenv(str(Path(__file__).parent.parent /
                        'bili-autopost-sau' / '.env'))
    cfg = load_config()
    store = Store(f"{DATA_DIR}/topics.db")

    # ① 主题（跳过做过的）
    topics = [t for i, t in enumerate(cfg["topics"], 1)
              if topic_index is None or i == topic_index]
    fresh = [t for t in topics if not store.is_topic_done(t)]
    if not fresh:
        log.warning("主题池已全部做完，请在 config.yaml 补充")
        return
    topic = random.choice(fresh)
    log.info("Episode topic: %s", topic)

    # ② 英文讲稿（两步：outline → 逐段扩写）
    vc = cfg["video"]
    doc = script_mod.generate(
        os.environ["ZHIPU_API_KEY"], topic,
        model=cfg["copywriter"]["model"], temperature=cfg["copywriter"]["temperature"],
        n_min=vc["sections_min"], n_max=vc["sections_max"],
        c_lo=vc["chars_per_section"][0], c_hi=vc["chars_per_section"][1])
    secs = doc["sections"]
    log.info("Script ready: %s | %d sections / %d words",
             doc["title"], len(secs), sum(len(s["text"].split()) for s in secs))

    # ③ 英文配音
    narr = cfg["narrator"]
    voices = narrator.narrate(
        secs, AUD_DIR, narr["voice"], narr.get("rate", "+0%"),
        backend=narr.get("backend", "edge"),
        allow_sapi=bool(narr.get("allow_sapi_fallback", True)))
    log.info("Narration done: %d sections / %.1fs",
             len(voices), sum(v["duration"] for v in voices))

    # ④ 实拍素材（MPT 方法：每段讲稿按 video_query 搜 Pexels 真实片段）
    from src import material
    from src import section_clip
    pex_key = os.environ.get("PEXELS_API_KEY", "")
    fade = float(cfg.get("transition", {}).get("fade_seconds", 0.6))
    section_files, credits, used_ids = [], [], set()
    for i, (sec, v) in enumerate(zip(secs, voices)):
        need = v["duration"] + (0.0 if i == len(secs) - 1 else fade)
        v["tail"] = 0.0 if i == len(secs) - 1 else fade
        got = material.search_best(pex_key, sec["image_query"], need, used_ids)
        raw = f"{DL_DIR}/raw_{i:02d}.mp4"
        if got:
            material.download(got[0], raw)
            credits.append(f"Pexels footage: '{sec['image_query']}'")
        else:                                          # 关键词失手 → 通用中国画面兜底
            got = material.search_best(pex_key, "china city scenery", need, used_ids)
            if not got:
                raise RuntimeError(f"section {i}: no footage for '{sec['image_query']}'")
            material.download(got[0], raw)
            credits.append(f"Pexels footage (fallback): '{sec['image_query']}'")
        clip = f"{DL_DIR}/sec_{i:02d}.mp4"
        section_clip.build(raw, v["duration"] + v["tail"], clip)
        section_files.append(clip)
        log.info("Section %d %.1fs footage='%s' (raw %ds)", i, v["duration"],
                 sec["image_query"], got[1])

    # ⑤ 合成（转场轮换 + ducking + 英文字幕）
    bgm_dir = Path(cfg.get("bgm_dir", "../bili-history/bgm"))
    bgms = sorted(bgm_dir.glob("*.mp3"))
    out_dir = Path("output") / re.sub(r"[^a-z0-9]+", "-",
                                      doc["title"].lower()).strip("-")[:60]
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / "final.mp4"
    srt = slideshow.make_srt(voices, str(out_dir / "final.srt"))
    slideshow.compose_final(section_files, str(random.choice(bgms)), srt, str(final),
                            bgm_volume=float(cfg["bgm_volume"]), fade=fade,
                            transitions=cfg.get("transition", {}).get("styles"))
    log.info("Episode: %s (%.1f MB)", final, os.path.getsize(final) / 1e6)

    # 封面截帧
    import subprocess, imageio_ffmpeg
    cover = out_dir / "cover.jpg"
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-ss", "2", "-i", str(final),
                    "-frames:v", "1", str(cover)], capture_output=True)

    # ⑥ upload_info.txt：手动上传 YouTube / Instagram 的全套素材
    tags = doc.get("tags", [])[:8] + cfg.get("hashtags", [])
    info = (
        f"TITLE: {doc['title']}\n\n"
        f"DESCRIPTION:\n{doc.get('description', '')}\n\n"
        f"A journey through real China — history, food, technology and daily life. "
        f"New episodes weekly. Subscribe and see China with your own eyes.\n\n"
        f"Image credits:\n" + ";\n".join(dict.fromkeys(credits)) +
        f"\n\nTAGS: {', '.join(dict.fromkeys(tags))}\n"
        f"IG_CAPTION: {doc['title']} 🇨🇳 #seechina #china #travelchina #chineseculture "
        f"#discoverchina #travel\n"
    )
    (out_dir / "upload_info.txt").write_text(info, encoding="utf-8")

    store.mark_topic(topic, doc["title"], "done")
    log.info("📦 产出目录: %s（final.mp4 + cover.jpg + upload_info.txt）", out_dir)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("topic", nargs="?", type=int, help="主题序号(从1)")
    args = ap.parse_args()
    setup_logging()
    run(args.topic)
