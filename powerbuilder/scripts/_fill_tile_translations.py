"""
One-shot helper: fill in Spanish and Vietnamese translations for the new
demo-carousel msgids. Idempotent — running it twice produces no change.

Vietnamese strings are flagged DRAFT (pending native-speaker review,
per the file-level translator note in locale/vi/LC_MESSAGES/django.po).

Run from /powerbuilder:
    ./venv/bin/python scripts/_fill_tile_translations.py
then recompile:
    msgfmt locale/es/LC_MESSAGES/django.po -o locale/es/LC_MESSAGES/django.mo
    msgfmt locale/vi/LC_MESSAGES/django.po -o locale/vi/LC_MESSAGES/django.mo
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent

# --- Translations -----------------------------------------------------------
# Keep keys identical to the English source (so makemessages can re-extract
# without losing context). One entry per chip / headline / preview / prompt.

ES = {
    # chips (1 to 2 words; keep short, sentence-case where natural)
    "Full plan":                       "Plan completo",
    "Win number":                      "Número de victoria",
    "Opposition":                      "Oposición",
    "Multilingual":                    "Multilingüe",
    "Social pack":                     "Paquete social",
    "Voter file":                      "Padrón electoral",
    # headlines
    "Gwinnett GOTV, Latinx 18 to 35":  "GOTV en Gwinnett, latinxs de 18 a 35",
    "Win number for GA-07, midterm":   "Número de victoria para GA-07, intermedias",
    "GOP opponent in GA-06":           "Rival republicano en GA-06",
    "Vietnamese text to AAPI voters":  "SMS en vietnamita a votantes AAPI",
    "Social media pack for GA-07 youth turnout":
        "Paquete de redes sociales para movilizar el voto joven en GA-07",
    "Segment my list and match scripts":
        "Segmenta mi lista y empareja los guiones",
    # previews
    "Spanish door-knock script and a CSV target list.":
        "Guion de puerta a puerta en español y un CSV de la lista objetivo.",
    "Quick lookup, shows CVAP and turnout math.":
        "Consulta rápida: muestra el CVAP y los cálculos de participación.",
    "Vulnerabilities and contrast angles from research books.":
        "Vulnerabilidades y ángulos de contraste a partir de los libros de investigación.",
    "Tests language detection plus messaging agent.":
        "Prueba la detección de idioma y el agente de mensajería.",
    "Pair with the Mobilization mode toggle for GOTV-shaped CTAs across all eight formats.":
        "Combínalo con el modo Movilización para llamadas a la acción tipo GOTV en los ocho formatos.",
    "Uses the attached synthetic voterfile in demo mode.":
        "Usa el padrón sintético adjunto en modo demo.",
    # prompts (multi-line in source; gettext joins them, so the msgid is one
    # long string. Translate as one paragraph.)
    "Build a Gwinnett County GOTV plan targeting Latinx voters age 18 to 35. "
    "Generate a Spanish door-knock script and give me a CSV of the target list.":
        "Crea un plan de GOTV en el condado de Gwinnett dirigido a votantes "
        "latinxs de 18 a 35 años. Genera un guion de puerta a puerta en "
        "español y entrégame un CSV de la lista objetivo.",
    "What is the win number for Georgia's 7th Congressional District in a "
    "midterm cycle? Show me the math.":
        "¿Cuál es el número de victoria para el 7.º distrito del Congreso "
        "de Georgia en un ciclo de elecciones intermedias? Muéstrame los "
        "cálculos.",
    "Pull opposition research on the Republican candidate in Georgia's 6th "
    "Congressional District. Give me the top vulnerabilities and three "
    "contrast messaging angles.":
        "Extrae investigación de oposición sobre el candidato republicano "
        "del 6.º distrito del Congreso de Georgia. Dame las principales "
        "vulnerabilidades y tres ángulos de mensaje de contraste.",
    "Draft a Vietnamese-language text message to AAPI voters in Gwinnett "
    "about early voting locations and hours.":
        "Redacta un mensaje de texto en vietnamita para votantes AAPI en "
        "Gwinnett sobre los lugares y horarios de votación anticipada.",
    "Build a social media pack for Georgia's 7th Congressional District "
    "youth turnout (18 to 29). Generate the Meta post, YouTube script, and "
    "TikTok script alongside the standard canvassing and phone outputs. "
    "Lead with cost-of-living framing where it fits the research. Tip: "
    "switch the input-bar Mode toggle to Mobilization for GOTV-shaped CTAs.":
        "Crea un paquete de redes sociales para movilizar el voto joven "
        "(18 a 29 años) en el 7.º distrito del Congreso de Georgia. Genera "
        "la publicación para Meta, el guion de YouTube y el guion de TikTok, "
        "junto con los materiales habituales de puerta a puerta y llamadas. "
        "Empieza con el encuadre de costo de vida cuando la investigación lo "
        "respalde. Consejo: cambia el selector de Modo a Movilización para "
        "obtener llamadas a la acción tipo GOTV.",
    "Segment my voter file by age cohort and party, then match each segment "
    "to the right canvassing script and turnout tactic.":
        "Segmenta mi padrón electoral por cohorte de edad y partido, y "
        "empareja cada segmento con el guion de puerta a puerta y la táctica "
        "de movilización adecuados.",
}

# Vietnamese — DRAFT, pending native-speaker review (per the translator note
# at the top of locale/vi/LC_MESSAGES/django.po). Tone: informal-respectful
# ("bạn"); keep proper nouns untranslated; AAPI / GOTV / CVAP / GA-07 / GA-06
# left as-is since they are domain terms organizers know.
VI = {
    "Full plan":                       "Kế hoạch đầy đủ",
    "Win number":                      "Số phiếu cần thắng",
    "Opposition":                      "Đối thủ",
    "Multilingual":                    "Đa ngôn ngữ",
    "Social pack":                     "Gói mạng xã hội",
    "Voter file":                      "Danh sách cử tri",
    "Gwinnett GOTV, Latinx 18 to 35":
        "Vận động đi bầu ở Gwinnett, cử tri Latinx 18 đến 35",
    "Win number for GA-07, midterm":
        "Số phiếu cần thắng cho GA-07, kỳ giữa nhiệm kỳ",
    "GOP opponent in GA-06":           "Đối thủ Cộng hòa ở GA-06",
    "Vietnamese text to AAPI voters":
        "Tin nhắn tiếng Việt cho cử tri AAPI",
    "Social media pack for GA-07 youth turnout":
        "Gói mạng xã hội để vận động cử tri trẻ ở GA-07",
    "Segment my list and match scripts":
        "Phân nhóm danh sách và ghép kịch bản phù hợp",
    "Spanish door-knock script and a CSV target list.":
        "Kịch bản gõ cửa bằng tiếng Tây Ban Nha và danh sách mục tiêu dạng CSV.",
    "Quick lookup, shows CVAP and turnout math.":
        "Tra cứu nhanh: hiển thị CVAP và phép tính tỷ lệ đi bầu.",
    "Vulnerabilities and contrast angles from research books.":
        "Điểm yếu và các góc tương phản, lấy từ sổ nghiên cứu.",
    "Tests language detection plus messaging agent.":
        "Thử nghiệm phát hiện ngôn ngữ và tác nhân nhắn tin.",
    "Pair with the Mobilization mode toggle for GOTV-shaped CTAs across all eight formats.":
        "Kết hợp với chế độ Vận động để có lời kêu gọi hành động kiểu GOTV trên cả tám định dạng.",
    "Uses the attached synthetic voterfile in demo mode.":
        "Dùng tệp cử tri tổng hợp đính kèm trong chế độ demo.",
    "Build a Gwinnett County GOTV plan targeting Latinx voters age 18 to 35. "
    "Generate a Spanish door-knock script and give me a CSV of the target list.":
        "Hãy xây dựng kế hoạch vận động đi bầu ở quận Gwinnett, hướng đến "
        "cử tri Latinx từ 18 đến 35 tuổi. Tạo kịch bản gõ cửa bằng tiếng Tây "
        "Ban Nha và gửi cho tôi danh sách mục tiêu dạng CSV.",
    "What is the win number for Georgia's 7th Congressional District in a "
    "midterm cycle? Show me the math.":
        "Số phiếu cần thắng cho Khu vực Quốc hội số 7 của Georgia trong "
        "kỳ bầu cử giữa nhiệm kỳ là bao nhiêu? Hãy chỉ cho tôi cách tính.",
    "Pull opposition research on the Republican candidate in Georgia's 6th "
    "Congressional District. Give me the top vulnerabilities and three "
    "contrast messaging angles.":
        "Tra cứu nghiên cứu về ứng cử viên Cộng hòa ở Khu vực Quốc hội số 6 "
        "của Georgia. Liệt kê những điểm yếu chính và ba góc tin nhắn "
        "tương phản.",
    "Draft a Vietnamese-language text message to AAPI voters in Gwinnett "
    "about early voting locations and hours.":
        "Hãy soạn một tin nhắn tiếng Việt gửi cử tri AAPI ở Gwinnett về địa "
        "điểm và giờ bỏ phiếu sớm.",
    "Build a social media pack for Georgia's 7th Congressional District "
    "youth turnout (18 to 29). Generate the Meta post, YouTube script, and "
    "TikTok script alongside the standard canvassing and phone outputs. "
    "Lead with cost-of-living framing where it fits the research. Tip: "
    "switch the input-bar Mode toggle to Mobilization for GOTV-shaped CTAs.":
        "Hãy xây dựng gói mạng xã hội để vận động cử tri trẻ (18 đến 29 tuổi) "
        "ở Khu vực Quốc hội số 7 của Georgia. Tạo bài đăng Meta, kịch bản "
        "YouTube và kịch bản TikTok, cùng với các kết quả gõ cửa và gọi điện "
        "tiêu chuẩn. Mở đầu bằng góc nhìn chi phí sinh hoạt khi nghiên cứu "
        "ủng hộ điều đó. Mẹo: chuyển nút Chế độ trên thanh nhập sang Vận "
        "động để có lời kêu gọi hành động kiểu GOTV.",
    "Segment my voter file by age cohort and party, then match each segment "
    "to the right canvassing script and turnout tactic.":
        "Phân nhóm danh sách cử tri của tôi theo độ tuổi và đảng phái, rồi "
        "ghép từng nhóm với kịch bản gõ cửa và chiến thuật vận động đi bầu "
        "phù hợp.",
}

# --- Mechanics --------------------------------------------------------------
# .po format: a sequence of `msgid "..."` (possibly multi-line, continued by
# additional `"..."` lines) followed by `msgstr "..."` (same shape). We parse
# entries, find the ones matching our English source, and rewrite their
# msgstr with our translation. We preserve everything else verbatim.

ENTRY_RE = re.compile(
    r'(msgid (?:"[^"\n]*"\s*\n?)+)(msgstr (?:"[^"\n]*"\s*\n?)+)',
    re.MULTILINE,
)


def _decode_msgid(block: str) -> str:
    """Concatenate all `"..."` chunks of a msgid block into one Python string."""
    parts = re.findall(r'"((?:\\.|[^"\\])*)"', block)
    # PO escapes: \n \t \" \\
    raw = "".join(parts)
    return (
        raw.replace(r"\n", "\n")
           .replace(r"\t", "\t")
           .replace(r"\"", '"')
           .replace(r"\\", "\\")
    )


def _encode_msgstr(s: str) -> str:
    """Render a Python string as a multi-line `msgstr "..."` block."""
    # Conservative: emit a single line for short strings, multi-line for long
    # ones (PO convention: empty first line, then 76-ish-col continuation).
    escaped = (
        s.replace("\\", r"\\")
         .replace('"', r"\"")
         .replace("\n", r"\n")
    )
    if len(escaped) <= 70:
        return f'msgstr "{escaped}"\n'
    # Wrap into ~70-char chunks at word boundaries.
    chunks: list[str] = []
    cur = ""
    for word in escaped.split(" "):
        candidate = (cur + " " + word) if cur else word
        if len(candidate) > 70:
            chunks.append(cur)
            cur = word
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    body = "".join(f'"{c} "\n' if i < len(chunks) - 1 else f'"{c}"\n'
                   for i, c in enumerate(chunks))
    return 'msgstr ""\n' + body


def patch_po(po_path: Path, table: dict[str, str]) -> int:
    src = po_path.read_text()
    n_filled = 0

    def replace(match: re.Match) -> str:
        nonlocal n_filled
        msgid_block = match.group(1)
        msgstr_block = match.group(2)
        msgid = _decode_msgid(msgid_block)
        if msgid in table:
            new_msgstr = _encode_msgstr(table[msgid])
            n_filled += 1
            return msgid_block + new_msgstr
        return match.group(0)

    new_src = ENTRY_RE.sub(replace, src)
    if new_src != src:
        po_path.write_text(new_src)
    return n_filled


def main() -> int:
    es_path = PROJECT_DIR / "locale" / "es" / "LC_MESSAGES" / "django.po"
    vi_path = PROJECT_DIR / "locale" / "vi" / "LC_MESSAGES" / "django.po"

    es_n = patch_po(es_path, ES)
    vi_n = patch_po(vi_path, VI)

    print(f"Spanish:    filled {es_n} / {len(ES)} demo-tile msgstr entries")
    print(f"Vietnamese: filled {vi_n} / {len(VI)} demo-tile msgstr entries")

    if es_n != len(ES) or vi_n != len(VI):
        print("WARN: some msgids were not found in the .po file. Re-run "
              "makemessages and check that the English source matches.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
