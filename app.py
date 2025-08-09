import io
import json
import re
from datetime import datetime
from pathlib import Path

import streamlit as st
from PIL import Image
from azure.core.credentials import AzureKeyCredential
from azure.ai.documentintelligence import DocumentIntelligenceClient
from openai import AzureOpenAI

# ---------------------------
# Streamlit page config
# ---------------------------
st.set_page_config(
    page_title="Notes → OCR → Quiz → AMP",
    page_icon="🧠",
    layout="centered"
)
st.title("🧠 Notes/Quiz OCR → GPT Structuring → AMP Web Story")
st.caption("Upload notes image(s) or a pre-made quiz image (or JSON), plus an AMP HTML template → download timestamped final HTML.")

# ---------------------------
# Secrets / Config (from st.secrets)
# ---------------------------
try:
    AZURE_DI_ENDPOINT = st.secrets["AZURE_DI_ENDPOINT"]      # e.g., https://<your-di>.cognitiveservices.azure.com/
    AZURE_API_KEY = st.secrets["AZURE_API_KEY"]

    AZURE_OPENAI_ENDPOINT = st.secrets["AZURE_OPENAI_ENDPOINT"]  # e.g., https://<your-openai>.openai.azure.com/
    AZURE_OPENAI_API_VERSION = st.secrets.get("AZURE_OPENAI_API_VERSION", "2024-08-01-preview")
    AZURE_OPENAI_API_KEY = st.secrets.get("AZURE_OPENAI_API_KEY", AZURE_API_KEY)  # reuse if same key
    GPT_DEPLOYMENT = st.secrets.get("GPT_DEPLOYMENT", "gpt-4o-mini")  # put your deployment name
except Exception:
    st.error("Missing secrets. Please set AZURE_DI_ENDPOINT, AZURE_API_KEY, AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_VERSION, AZURE_OPENAI_API_KEY, and GPT_DEPLOYMENT in secrets.")
    st.stop()

# ---------------------------
# Clients
# ---------------------------
di_client = DocumentIntelligenceClient(
    endpoint=AZURE_DI_ENDPOINT,
    credential=AzureKeyCredential(AZURE_API_KEY)
)

gpt_client = AzureOpenAI(
    api_key=AZURE_OPENAI_API_KEY,
    api_version=AZURE_OPENAI_API_VERSION,
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
)

# ---------------------------
# Prompts
# ---------------------------
SYSTEM_PROMPT_OCR_TO_QA = """
You receive OCR text that already contains multiple-choice questions in Hindi or English.
Each question has options (A)-(D), a single correct answer, and ideally an explanation.

Return a JSON object:
{
  "questions": [
    {
      "question": "...",
      "options": {"A":"...", "B":"...", "C":"...", "D":"..."},
      "correct_option": "A" | "B" | "C" | "D",
      "explanation": "..."
    },
    ...
  ]
}

- If explanations are missing, write a concise 1–2 sentence explanation grounded in the text.
- Preserve the original language (Hindi stays Hindi, English stays English).
- Ensure valid JSON only.
"""

SYSTEM_PROMPT_NOTES_TO_QA = """
You are given raw study notes text (could be Hindi or English). Generate exactly FIVE high-quality
multiple-choice questions (MCQs) that are strictly grounded in these notes.

For each question:
- Provide four options labeled A–D.
- Ensure exactly one correct option.
- Add a 1–2 sentence explanation that justifies the correct answer using the notes.

Respond ONLY with valid JSON in this schema:
{
  "questions": [
    {
      "question": "...",
      "options": {"A":"...", "B":"...", "C":"...", "D":"..."},
      "correct_option": "A" | "B" | "C" | "D",
      "explanation": "..."
    },
    ...
  ]
}

Language: Use the same language as the notes (auto-detect). Keep questions concise and unambiguous.
"""

SYSTEM_PROMPT_QA_TO_PLACEHOLDERS = """
You are given a JSON object with key "questions": a list where each item has:
- question (string)
- options: {"A":..., "B":..., "C":..., "D":...}
- correct_option (A/B/C/D)
- explanation (string)

Produce a single flat JSON object with EXACTLY these keys (sensible short defaults if missing).
Use the SAME language as the input questions (auto-detect; Hindi → Hindi, English → English).

pagetitle, storytitle, typeofquiz, potraitcoverurl,
s1title1, s1text1,

s2questionHeading, s2question1,
s2option1, s2option1attr, s2option2, s2option2attr,
s2option3, s2option3attr, s2option4, s2option4attr,
s2attachment1,

s3questionHeading, s3question1,
s3option1, s3option1attr, s3option2, s3option2attr,
s3option3, s3option3attr, s3option4, s3option4attr,
s3attachment1,

s4questionHeading, s4question1,
s4option1, s4option1attr, s4option2, s4option2attr,
s4option3, s4option3attr, s4option4, s4option4attr,
s4attachment1,

s5questionHeading, s5question1,
s5option1, s5option1attr, s5option2, s5option2attr,
s5option3, s5option3attr, s5option4, s5option4attr,
s5attachment1,

s6questionHeading, s6question1,
s6option1, s6option1attr, s6option2, s6option2attr,
s6option3, s6option3attr, s6option4, s6option4attr,
s6attachment1,

results_bg_image, results_prompt_text, results1_text, results2_text, results3_text

Mapping rules:
- We only need FIVE questions in the AMP template: map questions[0] → s2*, questions[1] → s3*, … questions[4] → s6*.
- sNquestion1 ← questions[N-2].question  (N=2..6)
- sNoption1..4 ← options A..D text
- For the correct option, set sNoptionKattr to the string "correct"; for others set "".
- sNattachment1 ← explanation for that question
- sNquestionHeading ← "Question {N-1}" (or the language-appropriate equivalent; e.g., Hindi: "प्रश्न {N-1}")
- pagetitle/storytitle: create short, relevant titles from the overall content.
- typeofquiz: "Educational" (or "शैक्षिक" in Hindi) if unknown.
- s1title1: a 2–5 word intro title; s1text1: 1–2 sentence intro.
- results_*: short friendly strings in the same language. results_bg_image: "" if none.

Return only the JSON object.
""".strip()

# ---------------------------
# Helpers
# ---------------------------
def clean_model_json(txt: str) -> str:
    """Remove code fences if model returns ```json ... ``` or ``` ... ```."""
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", txt, flags=re.DOTALL)
    if fenced:
        return fenced[0].strip()
    return txt.strip()

def ocr_extract(image_bytes: bytes) -> str:
    """OCR via Azure Document Intelligence prebuilt-read for one image."""
    poller = di_client.begin_analyze_document(
        model_id="prebuilt-read",
        body=image_bytes
    )
    result = poller.result()
    if getattr(result, "paragraphs", None):
        return "\n".join([p.content for p in result.paragraphs]).strip()
    if getattr(result, "content", None):
        return result.content.strip()
    lines = []
    for page in getattr(result, "pages", []) or []:
        for line in getattr(page, "lines", []) or []:
            if getattr(line, "content", None):
                lines.append(line.content)
    return "\n".join(lines).strip()

def ocr_extract_many(images_bytes_list) -> str:
    """OCR multiple images and concatenate with page separators."""
    chunks = []
    for idx, b in enumerate(images_bytes_list, start=1):
        text = ocr_extract(b)
        if text:
            chunks.append(f"[[PAGE {idx}]]\n{text}")
    return "\n\n".join(chunks).strip()

def gpt_ocr_text_to_questions(raw_text: str) -> dict:
    """Convert OCR text that already contains questions into structured questions JSON."""
    resp = gpt_client.chat.completions.create(
        model=GPT_DEPLOYMENT,
        temperature=0,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT_OCR_TO_QA},
            {"role": "user", "content": raw_text}
        ],
    )
    content = clean_model_json(resp.choices[0].message.content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, flags=re.DOTALL)
        if not m:
            raise
        return json.loads(m.group(0))

def gpt_notes_to_questions(notes_text: str) -> dict:
    """Generate 5 MCQs from raw notes text."""
    resp = gpt_client.chat.completions.create(
        model=GPT_DEPLOYMENT,
        temperature=0,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT_NOTES_TO_QA},
            {"role": "user", "content": notes_text}
        ],
    )
    content = clean_model_json(resp.choices[0].message.content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, flags=re.DOTALL)
        if not m:
            raise
        return json.loads(m.group(0))

def gpt_questions_to_placeholders(questions_data: dict) -> dict:
    """Map structured questions JSON into flat placeholder JSON for AMP template."""
    # Keep only first 5 (template supports 5 Qs → s2..s6)
    q = questions_data.get("questions", [])
    if len(q) > 5:
        questions_data = {"questions": q[:5]}
    resp = gpt_client.chat_completions.create(  # fallback alias if older client; try normal call first
        model=GPT_DEPLOYMENT,
        temperature=0,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT_QA_TO_PLACEHOLDERS},
            {"role": "user", "content": json.dumps(questions_data, ensure_ascii=False)}
        ],
    ) if hasattr(gpt_client, "chat_completions") else gpt_client.chat.completions.create(
        model=GPT_DEPLOYMENT,
        temperature=0,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT_QA_TO_PLACEHOLDERS},
            {"role": "user", "content": json.dumps(questions_data, ensure_ascii=False)}
        ],
    )
    # Normalize response object
    choice_msg = getattr(resp.choices[0], "message", getattr(resp.choices[0], "delta", None))
    content = clean_model_json(choice_msg.content if choice_msg else resp.choices[0].message.content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, flags=re.DOTALL)
        if not m:
            raise
        return json.loads(m.group(0))

def build_attr_value(key: str, val: str) -> str:
    """
    s2option3attr + "correct" → "option-3-correct", else "" or passthrough.
    """
    if not key.endswith("attr") or not val:
        return ""
    m = re.match(r"s(\d+)option(\d)attr$", key)
    if m and val.strip().lower() == "correct":
        return f"option-{m.group(2)}-correct"
    return val

def fill_template(template: str, data: dict) -> str:
    """Replace {{key}} and {{key|safe}} using placeholder data, handling *attr keys specially."""
    rendered = {}
    for k, v in data.items():
        if k.endswith("attr"):
            rendered[k] = build_attr_value(k, str(v))
        else:
            rendered[k] = "" if v is None else str(v)
    html = template
    for k, v in rendered.items():
        html = html.replace(f"{{{{{k}}}}}", v)
        html = html.replace(f"{{{{{k}|safe}}}}", v)
    return html

# ---------------------------
# 🧩 Builder UI
# ---------------------------
tab_all, = st.tabs(["All-in-one Builder"])

with tab_all:
    st.subheader("Build final AMP HTML from image(s) or structured JSON")
    st.caption("Pick an input source, upload AMP HTML template, and download the final HTML with a timestamped filename.")

    mode = st.radio(
        "Choose input",
        [
            "Notes image(s) (OCR → generate quiz JSON)",
            "Quiz image (OCR → parse existing MCQs)",
            "Structured JSON (skip OCR)"
        ],
        horizontal=False
    )

    up_tpl = st.file_uploader("📎 Upload AMP HTML template (.html)", type=["html", "htm"], key="tpl")
    show_debug = st.toggle("Show OCR / JSON previews", value=False)

    questions_data = None

    if mode == "Notes image(s) (OCR → generate quiz JSON)":
        up_imgs = st.file_uploader(
            "📎 Upload notes image(s) (JPG/PNG/WebP/TIFF) — multiple allowed",
            type=["jpg", "jpeg", "png", "webp", "tiff"],
            accept_multiple_files=True,
            key="notes_imgs"
        )
        if up_imgs:
            # Preview thumbnails
            if show_debug:
                for i, f in enumerate(up_imgs, start=1):
                    try:
                        st.image(Image.open(io.BytesIO(f.getvalue())).convert("RGB"),
                                 caption=f"Notes page {i}", use_container_width=True)
                    except Exception:
                        pass
            try:
                with st.spinner("🔍 OCR (Azure Document Intelligence) on all pages…"):
                    all_bytes = [f.getvalue() for f in up_imgs]
                    notes_text = ocr_extract_many(all_bytes)
                if not notes_text.strip():
                    st.error("OCR returned empty text. Try clearer images.")
                    st.stop()
                if show_debug:
                    with st.expander("📄 OCR Notes Text"):
                        st.text(notes_text[:8000] if len(notes_text) > 8000 else notes_text)

                with st.spinner("📝 Generating 5 MCQs from notes…"):
                    questions_data = gpt_notes_to_questions(notes_text)
                if show_debug:
                    with st.expander("🧱 Generated Questions JSON"):
                        st.code(json.dumps(questions_data, ensure_ascii=False, indent=2)[:8000], language="json")
            except Exception as e:
                st.error(f"Failed to process notes → quiz JSON: {e}")
                st.stop()

    elif mode == "Quiz image (OCR → parse existing MCQs)":
        up_img = st.file_uploader("📎 Upload quiz image (JPG/PNG)", type=["jpg", "jpeg", "png"], key="quiz_img")
        if up_img:
            img_bytes = up_img.getvalue()
            try:
                if show_debug:
                    st.image(Image.open(io.BytesIO(img_bytes)).convert("RGB"), caption="Uploaded quiz image", use_container_width=True)
                with st.spinner("🔍 OCR (Azure Document Intelligence)…"):
                    raw_text = ocr_extract(img_bytes)
                if not raw_text.strip():
                    st.error("OCR returned empty text. Try a clearer image.")
                    st.stop()
                if show_debug:
                    with st.expander("📄 OCR Text"):
                        st.text(raw_text[:8000] if len(raw_text) > 8000 else raw_text)
                with st.spinner("🤖 Parsing OCR into questions JSON…"):
                    questions_data = gpt_ocr_text_to_questions(raw_text)
                if show_debug:
                    with st.expander("🧱 Structured Questions JSON"):
                        st.code(json.dumps(questions_data, ensure_ascii=False, indent=2)[:8000], language="json")
            except Exception as e:
                st.error(f"Failed to process image → JSON: {e}")
                st.stop()

    else:  # Structured JSON
        up_json = st.file_uploader("📎 Upload structured questions JSON", type=["json"], key="json")
        if up_json:
            try:
                questions_data = json.loads(up_json.getvalue().decode("utf-8"))
                if show_debug:
                    with st.expander("🧱 Structured Questions JSON"):
                        st.code(json.dumps(questions_data, ensure_ascii=False, indent=2)[:8000], language="json")
            except Exception as e:
                st.error(f"Invalid JSON: {e}")
                st.stop()

    build = st.button("🛠️ Build final HTML", disabled=not (questions_data and up_tpl))

    if build and questions_data and up_tpl:
        try:
            # → placeholders
            with st.spinner("🧩 Generating placeholders…"):
                placeholders = gpt_questions_to_placeholders(questions_data)
                if show_debug:
                    with st.expander("🧩 Placeholder JSON"):
                        st.code(json.dumps(placeholders, ensure_ascii=False, indent=2)[:12000], language="json")

            # read template
            template_html = up_tpl.getvalue().decode("utf-8")

            # merge
            final_html = fill_template(template_html, placeholders)

            # save timestamped file
            ts_name = f"final_quiz_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
            Path(ts_name).write_text(final_html, encoding="utf-8")

            st.success(f"✅ Final HTML generated and saved as **{ts_name}**")
            with st.expander("🔍 HTML Preview (source)"):
                st.code(final_html[:120000], language="html")

            st.download_button(
                "⬇️ Download final HTML",
                data=final_html.encode("utf-8"),
                file_name=ts_name,
                mime="text/html"
            )

            st.info("AMP pages often won’t render inside Streamlit due to sandboxing/CSP. Download and open locally or deploy.")
        except Exception as e:
            st.error(f"Build failed: {e}")
    elif not (questions_data and up_tpl):
        st.info("Upload an input (notes/quiz image(s) or JSON) **and** a template to enable the Build button.")
