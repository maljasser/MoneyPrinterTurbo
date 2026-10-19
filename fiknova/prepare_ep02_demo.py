"""Build original, explicitly simulated EP02 footage; never approves or uploads.

Requires Pillow with RAQM and ffmpeg/ffprobe. No LLM, TTS or network is called.
The independent silent editorial preview is not a pipeline.render output.
Run from repository root: python fiknova/prepare_ep02_demo.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, features

ROOT = Path(__file__).resolve().parents[1]
W, H, FPS = 1080, 1920, 24
BG, PANEL = "#071B29", "#102C3B"
WHITE, MUTED, TEAL, AMBER, RED = "#F3F8FA", "#B3C8D3", "#55E0C1", "#FFD285", "#FF9A91"


def build(output: Path, font: Path) -> dict:
    if not features.check("raqm"):
        raise RuntimeError("Pillow RAQM is required for Arabic shaping")
    for binary in ("ffmpeg", "ffprobe"):
        if not shutil.which(binary):
            raise RuntimeError(f"Missing {binary}")
    data = json.loads((ROOT / "fiknova/ep02_meeting.sample.json").read_text())
    assert data["fictional"] is True
    assert data["actions"][1]["due_at"] is None
    source_ids = {n["id"] for n in data["notes"]}
    assert all(item["source"] in source_ids for group in ("confirmed_decisions", "actions", "needs_verification") for item in data[group])
    output.mkdir(parents=True, exist_ok=True)
    assets = ROOT / "fiknova/assets"
    assets.mkdir(parents=True, exist_ok=True)
    frames = output / "frames"
    frames.mkdir(exist_ok=True)
    enfont = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    if not enfont.exists():
        enfont = font

    def draw_scene(index, section, title, subtitle, cards, caption):
        im = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(im)
        def text(value, xy, size=44, color=WHITE, english=False, anchor="ra"):
            f = ImageFont.truetype(str(enfont if english else font), size)
            d.text(xy, value, font=f, fill=color, anchor=anchor,
                   direction="ltr" if english else "rtl", language="en" if english else "ar")
        def wrap(value, size, max_width=856):
            f = ImageFont.truetype(str(font), size)
            lines, line = [], ""
            for word in value.split():
                candidate = (line + " " + word).strip()
                if d.textlength(candidate, font=f, direction="rtl") > max_width and line:
                    lines.append(line)
                    line = word
                else:
                    line = candidate
            return lines + ([line] if line else [])
        d.rectangle((0, 0, W, 12), fill=TEAL)
        text("FIKNOVA", (64, 73), 39, TEAL, True, "la")
        text("EP 02  /  REVIEW", (1016, 83), 24, MUTED, True)
        d.rounded_rectangle((64, 148, 1016, 256), radius=22, fill="#163C49")
        text("محاكاة تعليمية • بيانات افتراضية", (978, 158), 39, TEAL)
        text("ليست تسجيلًا لأداة فعلية", (978, 207), 31, MUTED)
        text(section, (1016, 300), 34, TEAL)
        title_lines = wrap(title, 65, 930)
        y = 356
        for line in title_lines:
            text(line, (1016, y), 65)
            y += 83
        for line in wrap(subtitle, 36, 930):
            text(line, (1016, y + 22), 36, MUTED)
            y += 49
        y = max(610, y + 65)
        for label, body, color in cards:
            lines = wrap(body, 43)
            height = 98 + 61 * len(lines)
            if y + height > 1510:
                raise ValueError(f"Scene {index}: card overflow")
            d.rounded_rectangle((64, y, 1016, y + height), radius=25, fill=PANEL)
            d.rounded_rectangle((998, y + 24, 1005, y + height - 24), radius=3, fill=color)
            text(label, (968, y + 17), 32, color)
            for j, line in enumerate(lines):
                text(line, (968, y + 73 + j * 61), 43)
            y += height + 22
        d.rounded_rectangle((64, 1565, 1016, 1792), radius=24, fill="#183D4D")
        text("ملاحظة للمشاهد", (970, 1585), 28, TEAL)
        for j, line in enumerate(wrap(caption, 41)):
            if j >= 3:
                raise ValueError("Caption overflow")
            text(line, (970, 1633 + j * 53), 41)
        text("مسودة بصرية صامتة • بانتظار مراجعتك", (1016, 1823), 30, AMBER)
        for n in range(8):
            d.rounded_rectangle((64 + n*120, 1881, 166 + n*120, 1888), radius=3,
                                fill=TEAL if n <= index else "#284553")
        path = frames / f"scene-{index+1:02d}.png"
        im.save(path)
        return path

    scenes = [
        ("النتيجة أولًا", "من الملاحظات إلى التنفيذ", "مثال مصمم للتوضيح؛ لم نختبر أداة AI هنا.", [
            ("قرار مؤكد • N01", "تجربة النموذج مع 5 مستخدمين قبل التوسع", TEAL),
            ("مهمتان • N02 / N03", "سارة: تجهيز النموذج | عمر: مراجعة التكلفة", TEAL),
            ("4 عناصر تحتاج تحقق", "الموعد، الميزانية، الإطلاق، وبيانات العملاء", AMBER)],
         "كل مخرج مرتبط بملاحظة. لا يصبح الاقتراح قرارًا لمجرد ظهوره في الملخص."),
        ("01 / الملاحظات", "ماذا قيل فعلًا؟", "اجتماع افتراضي: 20 أكتوبر 2026 • الرياض", [
            ("N01 • اتفاق صريح", data["notes"][0]["text"], TEAL),
            ("N02 • التزام واضح", data["notes"][1]["text"], TEAL)],
         "نحافظ على العدد والاسم والتاريخ والوقت كما وردت في الملاحظات."),
        ("01 / الملاحظات", "ما الذي لم يُحسم؟", "المعلومات الغائبة لا تعني تفويضًا بالتخمين.", [
            ("N03 • موعد غائب", "عمر سيتولى مراجعة التكلفة؛ لم يذكر موعدًا.", AMBER),
            ("N04 / N05 • مقترحات فقط", "50 ألف ريال للميزانية، و1 نوفمبر للإطلاق؛ لم يعتمدا.", AMBER),
            ("N06 • سؤال مفتوح", "استخدام بيانات العملاء: غير محسوم، والمسؤول غير محدد.", AMBER)],
         "هذه نقاط تحقق، وليست قرارات معتمدة أو مهام مكتملة البيانات."),
        ("02 / طلب الاستخراج", "اطلب 3 مخرجات فقط", "تعليمات قابلة للاستخدام؛ المخرجات التالية محاكاة.", [
            ("1 • القرارات", "استخرج القرارات الصريحة فقط، مع رقم الملاحظة.", TEAL),
            ("2 • المهام", "حدد المهمة والمسؤول والموعد؛ اكتب غير محدد عند غياب أي منها.", TEAL),
            ("3 • التحقق", "افصل الأسئلة المفتوحة والمقترحات غير المعتمدة.", AMBER)],
         "لا تضف موعدًا أو مسؤولًا، ولا تحول اقتراحًا إلى قرار."),
        ("03 / خطأ توضيحي", "موعد يبدو منطقيًا... لكنه مختلق", "خطأ صممناه للتعليم؛ ليس ناتجًا مرصودًا لأداة.", [
            ("المخرج الخاطئ • A02", "عمر يراجع التكلفة بحلول 23 أكتوبر 2026.", RED),
            ("دليل المراجعة • N03", "الملاحظات لم تذكر أي موعد لهذه المهمة.", AMBER),
            ("الإجراء الصحيح", "احذف 23 أكتوبر. انقل سؤال الموعد إلى قائمة التحقق.", TEAL)],
         "دقة الصياغة لا تثبت صحة المعلومة؛ ارجع إلى الملاحظة الأصلية."),
        ("04 / المخرجات المصححة", "قرار واحد ومهمتان", "مخرجات المثال بعد المراجعة مقابل الملاحظات.", [
            ("D01 • المصدر N01", "تجربة النموذج مع 5 مستخدمين قبل التوسع.", TEAL),
            ("A01 • المصدر N02", "سارة: النموذج الأولي — 22 أكتوبر 2026، 3 مساء بالرياض.", TEAL),
            ("A02 • المصدر N03", "عمر: مراجعة التكلفة — الموعد غير محدد.", AMBER)],
         "تحديد القرار داخل المثال لا يعني اعتماد الحلقة أو السماح بالنشر."),
        ("05 / قائمة التحقق", "4 نقاط قبل التنفيذ", "لا ترسل الملخص قبل مراجعة الفريق.", [
            ("Q01 / Q02 • N03 / N04", "تأكيد موعد عمر، واعتماد الميزانية؛ 50 ألف ريال اقتراح فقط.", AMBER),
            ("Q03 • N05", "تأكيد موعد الإطلاق؛ 1 نوفمبر 2026 غير معتمد.", AMBER),
            ("Q04 • N06", "حسم استخدام بيانات العملاء، وتعيين مسؤول للتحقق.", AMBER)],
         "نصنف الغموض بوضوح، ونترك الحسم لأصحاب الصلاحية."),
        ("06 / المراجعة البشرية", "راجع. أكد. ثم شارك.", "Fiknova • من الفكرة إلى القيمة", [
            ("فحص سريع", "هل لكل قرار دليل؟ وهل المسؤول والموعد مذكوران فعلًا؟", TEAL),
            ("قبل المشاركة", "أكد النقاط المفتوحة مع الفريق وافحص خصوصية البيانات.", TEAL),
            ("حالة الحلقة", "مسودة للمراجعة فقط — لم تعتمد ولم تنشر.", AMBER)],
         "أي خطوة في اجتماعاتك تستغرق منك وقتًا أطول؟")
    ]
    segment_paths = []
    for index, scene in enumerate(scenes):
        still = draw_scene(index, *scene)
        clip = frames / f"scene-{index+1:02d}.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-loop", "1", "-framerate", str(FPS), "-i", str(still), "-t", "8", "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart", str(clip)], check=True)
        segment_paths.append(clip)
    def concat(paths, target, name):
        listing = frames / f"{name}.txt"
        listing.write_text("".join("file '" + str(p).replace("'", "'\\''") + "'\n" for p in paths))
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(target)], check=True)
    # Full preview is demo-first; two materials retain their requested filenames.
    concat(segment_paths[:4], assets / "ep02-notes-demo.mp4", "notes")
    concat(segment_paths[4:], assets / "ep02-output-demo.mp4", "output")
    preview = output / "Fiknova_EP02_Visual_Review.mp4"
    concat(segment_paths, preview, "review")
    for name in ("ep02-notes-demo.mp4", "ep02-output-demo.mp4"):
        shutil.copyfile(assets / name, output / name)
    record = {"type":"independent_silent_editorial_preview", "status":"draft", "approved":False,
              "published":False, "automatic_upload":False, "audio":"none; narration and synchronization pending",
              "not_a_real_tool_recording":True, "not_a_pipeline_render":True,
              "dimensions":[W,H], "fps":FPS, "duration_seconds":64,
              "source_commit":"0e6894636adc70478bb8da6e88b66634265cf7b5", "files":{}}
    for p in (preview, output / "ep02-notes-demo.mp4", output / "ep02-output-demo.mp4"):
        probe = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(p)]))
        record["files"][p.name] = {"sha256":hashlib.sha256(p.read_bytes()).hexdigest(), "size_bytes":p.stat().st_size,
                                  "duration_seconds":float(probe["format"]["duration"]),
                                  "video_streams":[{k:s[k] for k in ("codec_name","width","height","pix_fmt","r_frame_rate")} for s in probe["streams"] if s["codec_type"] == "video"]}
    (output / "review.json").write_text(json.dumps(record, ensure_ascii=False, indent=2))
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "storage/fiknova/ep02-preparation")
    parser.add_argument("--font", type=Path, default=ROOT / "resource/fonts/NotoNaskhArabic-Regular.ttf")
    args = parser.parse_args()
    print(json.dumps(build(args.output.resolve(), args.font.resolve()), ensure_ascii=False, indent=2))
