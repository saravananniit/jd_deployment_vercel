# """Resume vs. job description fit checker: FastAPI + Ollama (local) or Groq (bring your own key).

# Mirrors the n8n workflow:
#   form submission -> extract JD text + extract resume text -> merge -> LLM -> result

# The user picks the provider, model name and (for Groq) API key in the web page. They are sent
# with each request and used only for that request: the server never stores or logs them, and it
# never uses a server-side Groq key, so token cost goes to whoever typed their key in.

# The fit rating is computed in Python, not by the model:
#   1. the model extracts the required skills from the JD (JSON),
#   2. plain Python checks which of those skills appear in the resume and computes the score,
#   3. the model writes the explanation and Python appends the rating.

# Optional environment variables (a local .env file works too, see .env.example):
#   ENABLED_PROVIDERS   comma list, default "ollama,groq". Use "groq" on a deployed server.
#   LLM_PROVIDER        provider preselected in the page (default: first enabled)
#   OLLAMA_URL, OLLAMA_MODEL, OLLAMA_NUM_CTX   Ollama server settings / default model name
#   GROQ_MODEL          model name prefilled in the page for Groq (optional)
#   GROQ_BASE_URL       override the Groq API base URL (optional)

# Run locally:
#     uv run uvicorn main:app --reload
# Deploy (behind HTTPS, because API keys travel in the request body):
#     uvicorn main:app --host 0.0.0.0 --port $PORT
# """

# import json
# import os
# import re
# from io import BytesIO
# from pathlib import Path
# from typing import AsyncIterator, Protocol

# import httpx
# from dotenv import load_dotenv
# from fastapi import FastAPI, File, Form, HTTPException, UploadFile
# from fastapi.responses import FileResponse, StreamingResponse
# from pypdf import PdfReader

# BASE_DIR = Path(__file__).parent
# # Real environment variables (e.g. set on your hosting platform) win over .env.
# load_dotenv(BASE_DIR / ".env")

# # Per-document character cap, so JD + resume + prompt + answer fit in the model's context.
# MAX_CHARS = 10_000
# MAX_SKILLS = 15

# ALL_PROVIDERS = ("ollama", "groq")
# ENABLED_PROVIDERS = [
#     p
#     for p in (x.strip().lower() for x in os.getenv("ENABLED_PROVIDERS", "ollama,groq").split(","))
#     if p in ALL_PROVIDERS
# ] or ["ollama"]
# _default = os.getenv("LLM_PROVIDER", "").strip().lower()
# DEFAULT_PROVIDER = _default if _default in ENABLED_PROVIDERS else ENABLED_PROVIDERS[0]
# DEFAULT_MODELS = {
#     "ollama": os.getenv("OLLAMA_MODEL", "gemma3:1b"),
#     "groq": os.getenv("GROQ_MODEL", ""),
# }

# MODEL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\-]{0,99}")
# API_KEY_RE = re.compile(r"[\x21-\x7E]{8,200}")  # printable ASCII, no spaces or newlines

# app = FastAPI(title="Resume vs JD fit checker")

# # ---------------------------------------------------------------- prompts

# SKILLS_PROMPT = """List the specific technical skills, programming languages, frameworks, tools, platforms and certifications that the job description below asks for.

# Rules:
# - Use short names of 1 to 3 words, for example "Python", "FastAPI", "AWS", "CI/CD".
# - Only include skills that are written in the job description. Do not invent any.
# - At most {max_skills} items.
# - Respond with JSON only, in this form: {{"skills": ["Python", "FastAPI"]}}

# ===== JOB DESCRIPTION =====
# {jd}
# ===== END OF JOB DESCRIPTION =====
# """

# SKILLS_SCHEMA = {
#     "type": "object",
#     "properties": {"skills": {"type": "array", "items": {"type": "string"}}},
#     "required": ["skills"],
# }

# REPORT_PROMPT = """You are an AI hiring assistant.

# You will receive two text inputs:
# 1. The JOB DESCRIPTION - a technical job description.
# 2. The RESUME - the resume of an applicant.

# ===== JOB DESCRIPTION =====
# {jd}
# ===== END OF JOB DESCRIPTION =====

# ===== RESUME =====
# {resume}
# ===== END OF RESUME =====

# A keyword check has already compared the job description's skills with the resume:
# Skills found in the resume: {matched}
# Skills not found in the resume: {missing}

# Your task is to:
# - Compare the resume against the Job Description.
# - Highlight which required skills and experience match.
# - Point out missing or unmatched skills/experience.

# Rules:
# - Under Skills Matched, list only the skills from "Skills found in the resume". Under Skills Missing, list only the skills from "Skills not found in the resume". Write None if a list is empty.
# - Base every statement only on the two documents above. Do not invent experience.
# - Do not write a fit rating. It is added automatically after your answer.

# Output Format:
# - Wherever a line break is needed, use <br> instead of \\n. Use <br> for a single line break and <br><br> for a double line break and so on and so forth.
# - Embed all headings between <strong> and </strong>.
# - Use no HTML tags other than <br> and <strong>.
# Use well-structured bullet points with **highlighted skill names**.
# Group your output under the following headings with spacing between each:

# <strong>📊 Match Summary</strong>

# <strong>✅ Skills Matched:</strong>
# - [Skill]: [why it matches]

# <strong>💼 Experience Matched:</strong>
# - [Experience area]: [why it matches]

# <strong>❌ Skills Missing:</strong>
# - [Missing Skill]: [Reason it's considered missing]

# <strong>⚠️ Experience Gaps:</strong>
# - [Gap]: [Explanation]

# Replace everything in square brackets with real content.
# """

# # ---------------------------------------------------------------- text extraction


# async def extract_text(upload: UploadFile, label: str) -> str:
#     """The 'Extract from PDF' node: return plain text from a PDF (or .txt) upload."""
#     data = await upload.read()
#     if not data:
#         raise HTTPException(400, f"The {label} file is empty.")

#     name = (upload.filename or "").lower()
#     try:
#         if name.endswith(".txt"):
#             text = data.decode("utf-8", errors="ignore")
#         else:
#             reader = PdfReader(BytesIO(data))
#             text = "\n".join((page.extract_text() or "") for page in reader.pages)
#     except Exception as exc:
#         raise HTTPException(
#             400, f"Couldn't read the {label}. Upload a text-based PDF or a .txt file."
#         ) from exc

#     text = text.strip()
#     if not text:
#         raise HTTPException(
#             422, f"No text found in the {label}. Scanned PDFs need OCR before upload."
#         )
#     return text[:MAX_CHARS]


# # ---------------------------------------------------------------- skill matching (plain Python)

# _SPECIAL = (("c++", " cpp "), ("c#", " csharp "), (".net", " dotnet "))


# def normalize(text: str) -> str:
#     """Lowercase, map C++/C#/.NET to plain tokens, drop punctuation, collapse spaces."""
#     text = text.lower()
#     for raw, repl in _SPECIAL:
#         text = text.replace(raw, repl)
#     return re.sub(r"[^a-z0-9]+", " ", text).strip()


# def contains_skill(haystack_norm: str, skill: str) -> bool:
#     """Whole-word match of a skill in normalized text, tolerant of plurals and 'node js' vs 'nodejs'."""
#     needle = normalize(skill)
#     if not needle:
#         return False
#     padded = f" {haystack_norm} "
#     variants = {needle}
#     if len(needle) > 3 and needle.endswith("s"):
#         variants.add(needle[:-1])
#     else:
#         variants.add(needle + "s")
#     if any(f" {v} " in padded for v in variants):
#         return True
#     # multi-word skills: "node js" should also match "nodejs"
#     if " " in needle:
#         compact = needle.replace(" ", "")
#         return len(compact) >= 5 and compact in haystack_norm.replace(" ", "")
#     return False


# def match_skills(skills: list[str], resume_text: str) -> tuple[list[str], list[str]]:
#     resume_norm = normalize(resume_text)
#     matched, missing = [], []
#     for skill in skills:
#         (matched if contains_skill(resume_norm, skill) else missing).append(skill)
#     return matched, missing


# # ---------------------------------------------------------------- LLM providers


# class LLMError(Exception):
#     """Raised with a message that is safe to show in the UI (never contains the API key)."""


# class Provider(Protocol):
#     name: str  # "ollama" | "groq"
#     model: str

#     async def ensure_ready(self) -> None: ...
#     async def json_completion(self, prompt: str, schema: dict) -> str: ...
#     def stream(self, prompt: str) -> AsyncIterator[str]: ...


# class OllamaProvider:
#     """Ollama server (default gemma3:1b). The server URL comes from the environment only."""

#     name = "ollama"

#     def __init__(self, model: str) -> None:
#         self.model = model
#         self.url = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
#         # Ollama's default context window is small and silently truncates long prompts.
#         self.num_ctx = int(os.getenv("OLLAMA_NUM_CTX", "8192"))

#     async def ensure_ready(self) -> None:
#         try:
#             async with httpx.AsyncClient(timeout=5) as client:
#                 resp = await client.get(f"{self.url}/api/tags")
#                 resp.raise_for_status()
#         except httpx.HTTPError as exc:
#             raise LLMError(
#                 f"Can't reach Ollama at {self.url}. Start it with `ollama serve`."
#             ) from exc
#         names = {m.get("name", "") for m in resp.json().get("models", [])}
#         if self.model not in names and f"{self.model}:latest" not in names:
#             raise LLMError(f"Model {self.model} isn't installed. Run `ollama pull {self.model}`.")

#     def _payload(self, prompt: str, **extra) -> dict:
#         return {"model": self.model, "messages": [{"role": "user", "content": prompt}], **extra}

#     async def json_completion(self, prompt: str, schema: dict) -> str:
#         payload = self._payload(
#             prompt,
#             stream=False,
#             format=schema,
#             options={"temperature": 0, "num_ctx": self.num_ctx},
#         )
#         try:
#             async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5)) as client:
#                 resp = await client.post(f"{self.url}/api/chat", json=payload)
#                 resp.raise_for_status()
#             return resp.json()["message"]["content"]
#         except (httpx.HTTPError, KeyError, ValueError) as exc:
#             raise LLMError(f"Ollama failed: {exc}") from exc

#     async def stream(self, prompt: str) -> AsyncIterator[str]:
#         payload = self._payload(
#             prompt, stream=True, options={"temperature": 0.2, "num_ctx": self.num_ctx}
#         )
#         try:
#             async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=5)) as client:
#                 async with client.stream("POST", f"{self.url}/api/chat", json=payload) as resp:
#                     if resp.status_code != 200:
#                         body = (await resp.aread()).decode("utf-8", errors="ignore")
#                         raise LLMError(f"Ollama returned {resp.status_code}: {body}")
#                     async for line in resp.aiter_lines():
#                         if not line:
#                             continue
#                         chunk = json.loads(line)
#                         if "error" in chunk:
#                             raise LLMError(str(chunk["error"]))
#                         yield chunk.get("message", {}).get("content", "")
#                         if chunk.get("done"):
#                             break
#         except httpx.HTTPError as exc:
#             raise LLMError(f"Lost connection to Ollama: {exc}") from exc


# class GroqProvider:
#     """Groq's OpenAI-compatible API, using the API key the user typed into the page."""

#     name = "groq"

#     def __init__(self, model: str, api_key: str) -> None:
#         self.model = model
#         self._api_key = api_key
#         self.base_url = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")

#     async def ensure_ready(self) -> None:
#         return None  # key and model are validated in build_provider; Groq checks them on first call

#     @property
#     def _headers(self) -> dict:
#         return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

#     @staticmethod
#     def _error(status: int, body: str) -> str:
#         if status == 401:
#             return "Groq rejected the API key (401). Check the key you entered."
#         try:
#             message = json.loads(body)["error"]["message"]
#         except (ValueError, KeyError, TypeError):
#             message = body[:200]
#         return f"Groq returned {status}: {message}"

#     def _payload(self, prompt: str, **extra) -> dict:
#         return {"model": self.model, "messages": [{"role": "user", "content": prompt}], **extra}

#     async def json_completion(self, prompt: str, schema: dict) -> str:
#         # JSON mode works on every Groq model (the prompt must mention JSON, which ours does).
#         payload = self._payload(prompt, temperature=0, response_format={"type": "json_object"})
#         try:
#             async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10)) as client:
#                 resp = await client.post(
#                     f"{self.base_url}/chat/completions", headers=self._headers, json=payload
#                 )
#             if resp.status_code != 200:
#                 raise LLMError(self._error(resp.status_code, resp.text))
#             return resp.json()["choices"][0]["message"]["content"] or ""
#         except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
#             raise LLMError(f"Groq request failed: {type(exc).__name__}") from exc

#     async def stream(self, prompt: str) -> AsyncIterator[str]:
#         payload = self._payload(prompt, temperature=0.2, stream=True)
#         try:
#             async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10)) as client:
#                 async with client.stream(
#                     "POST", f"{self.base_url}/chat/completions", headers=self._headers, json=payload
#                 ) as resp:
#                     if resp.status_code != 200:
#                         body = (await resp.aread()).decode("utf-8", errors="ignore")
#                         raise LLMError(self._error(resp.status_code, body))
#                     async for line in resp.aiter_lines():
#                         if not line.startswith("data:"):
#                             continue
#                         data = line[5:].strip()
#                         if data == "[DONE]":
#                             break
#                         chunk = json.loads(data)
#                         if "error" in chunk:
#                             raise LLMError(str(chunk["error"].get("message", chunk["error"])))
#                         choices = chunk.get("choices") or []
#                         if choices:
#                             delta = choices[0].get("delta", {}).get("content")
#                             if delta:
#                                 yield delta
#         except httpx.HTTPError as exc:
#             raise LLMError(f"Lost connection to Groq: {type(exc).__name__}") from exc


# def build_provider(name: str, model: str, api_key: str) -> Provider:
#     """Validate what the page sent and build the provider for this one request."""
#     name = (name or DEFAULT_PROVIDER).strip().lower()
#     if name not in ENABLED_PROVIDERS:
#         raise LLMError(f"The '{name}' provider isn't available on this server.")

#     model = (model or "").strip() or DEFAULT_MODELS.get(name, "")
#     if not model:
#         raise LLMError(f"Enter a model name for {name.capitalize()}.")
#     if not MODEL_NAME_RE.fullmatch(model):
#         raise LLMError("That model name has characters a model name can't contain.")

#     if name == "groq":
#         api_key = (api_key or "").strip()
#         if not api_key:
#             raise LLMError("Enter your Groq API key.")
#         if not API_KEY_RE.fullmatch(api_key):
#             raise LLMError("That doesn't look like a valid API key.")
#         return GroqProvider(model, api_key)
#     return OllamaProvider(model)


# # ---------------------------------------------------------------- pipeline


# async def extract_required_skills(provider: Provider, jd_text: str) -> list[str]:
#     """Step 1: ask the model for the JD's skills as JSON, then keep only ones really in the JD."""
#     prompt = SKILLS_PROMPT.format(jd=jd_text, max_skills=MAX_SKILLS)
#     content = await provider.json_completion(prompt, SKILLS_SCHEMA)
#     try:
#         raw_skills = json.loads(content).get("skills", [])
#     except (ValueError, AttributeError):
#         raw_skills = []

#     jd_norm = normalize(jd_text)
#     skills, seen = [], set()
#     for item in raw_skills:
#         skill = str(item).strip()
#         key = normalize(skill)
#         if not key or key in seen or len(skill) > 40:
#             continue
#         if not contains_skill(jd_norm, skill):  # guard against invented skills
#             continue
#         seen.add(key)
#         skills.append(skill)
#     return skills[:MAX_SKILLS]


# async def stream_report(provider: Provider, prompt: str, rating_block: str):
#     """Step 3: stream the model's explanation, then append the Python-computed rating."""
#     try:
#         async for token in provider.stream(prompt):
#             yield token
#     except LLMError as exc:
#         yield f"\n[[ERROR]] {exc}"
#         return
#     yield rating_block


# def build_rating_block(matched: list[str], total: int) -> str:
#     if total == 0:
#         return (
#             "\n\n<strong>🎯 Final Fit Rating:</strong>\n"
#             "Not available: no specific skills could be read from the job description.\n"
#         )
#     pct = round(100 * len(matched) / total)
#     return (
#         "\n\n<strong>🎯 Final Fit Rating:</strong>\n"
#         f"{pct}% ({len(matched)} of {total} required skills found in the resume)\n"
#     )


# # ---------------------------------------------------------------- routes


# @app.post("/api/match")
# async def match(
#     jd: UploadFile = File(...),
#     resume: UploadFile = File(...),
#     provider: str = Form(""),
#     model: str = Form(""),
#     api_key: str = Form(""),
# ):
#     try:
#         llm = build_provider(provider, model, api_key)
#     except LLMError as exc:
#         raise HTTPException(400, str(exc)) from exc

#     jd_text = await extract_text(jd, "job description")
#     resume_text = await extract_text(resume, "resume")

#     try:
#         await llm.ensure_ready()
#     except LLMError as exc:
#         raise HTTPException(503, str(exc)) from exc
#     try:
#         skills = await extract_required_skills(llm, jd_text)
#     except LLMError as exc:
#         raise HTTPException(502, str(exc)) from exc

#     matched, missing = match_skills(skills, resume_text)
#     prompt = REPORT_PROMPT.format(
#         jd=jd_text,
#         resume=resume_text,
#         matched=", ".join(matched) or "None",
#         missing=", ".join(missing) or "None",
#     )
#     return StreamingResponse(
#         stream_report(llm, prompt, build_rating_block(matched, len(skills))),
#         media_type="text/plain; charset=utf-8",
#         headers={"Cache-Control": "no-cache"},
#     )


# @app.get("/api/config")
# async def config():
#     """Defaults for the settings box. Never includes any key."""
#     return {
#         "providers": ENABLED_PROVIDERS,
#         "provider": DEFAULT_PROVIDER,
#         "models": DEFAULT_MODELS,
#     }


# @app.get("/")
# async def index():
#     return FileResponse(BASE_DIR / "index_final.html")

"""Resume vs. job description fit checker: FastAPI + Ollama (local) or Groq (bring your own key).

Mirrors the n8n workflow:
  form submission -> extract JD text + extract resume text -> merge -> LLM -> result

The user picks the provider, model name and (for Groq) API key in the web page. They are sent
with each request and used only for that request: the server never stores or logs them, and it
never uses a server-side Groq key, so token cost goes to whoever typed their key in.

The fit rating is computed in Python, not by the model:
  1. the model extracts the required skills from the JD (JSON),
  2. plain Python checks which of those skills appear in the resume and computes the score,
  3. the model writes the explanation and Python appends the rating.

Optional environment variables (a local .env file works too, see .env.example):
  ENABLED_PROVIDERS   comma list, default "ollama,groq". Use "groq" on a deployed server.
  LLM_PROVIDER        provider preselected in the page (default: first enabled)
  OLLAMA_URL, OLLAMA_MODEL, OLLAMA_NUM_CTX   Ollama server settings / default model name
  GROQ_MODEL          model name prefilled in the page for Groq (optional)
  GROQ_BASE_URL       override the Groq API base URL (optional)

Run locally:
    uv run uvicorn main:app --reload
Deploy (behind HTTPS, because API keys travel in the request body):
    uvicorn main:app --host 0.0.0.0 --port $PORT
"""

import json
import logging
import os
import re
from io import BytesIO
from pathlib import Path
from typing import AsyncIterator, Protocol

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pypdf import PdfReader

BASE_DIR = Path(__file__).parent
# Real environment variables (e.g. set on your hosting platform) win over .env.
load_dotenv(BASE_DIR / ".env")

# Per-document character cap, so JD + resume + prompt + answer fit in the model's context.
MAX_CHARS = 10_000
MAX_SKILLS = 15

ALL_PROVIDERS = ("ollama", "groq")
ENABLED_PROVIDERS = [
    p
    for p in (x.strip().lower() for x in os.getenv("ENABLED_PROVIDERS", "ollama,groq").split(","))
    if p in ALL_PROVIDERS
] or ["ollama"]
_default = os.getenv("LLM_PROVIDER", "").strip().lower()
DEFAULT_PROVIDER = _default if _default in ENABLED_PROVIDERS else ENABLED_PROVIDERS[0]
DEFAULT_MODELS = {
    "ollama": os.getenv("OLLAMA_MODEL", "gemma3:1b"),
    "groq": os.getenv("GROQ_MODEL", ""),
}

MODEL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\-]{0,99}")
API_KEY_RE = re.compile(r"[\x21-\x7E]{8,200}")  # printable ASCII, no spaces or newlines

log = logging.getLogger("uvicorn.error")

app = FastAPI(title="Resume vs JD fit checker")

# ---------------------------------------------------------------- prompts

SKILLS_PROMPT = """List the specific technical skills, programming languages, frameworks, tools, platforms and certifications that the job description below asks for.

Rules:
- Use short names of 1 to 3 words, for example "Python", "FastAPI", "AWS", "CI/CD".
- Only include skills that are written in the job description. Do not invent any.
- At most {max_skills} items.
- Respond with JSON only, in this form: {{"skills": ["Python", "FastAPI"]}}

===== JOB DESCRIPTION =====
{jd}
===== END OF JOB DESCRIPTION =====
"""

SKILLS_SCHEMA = {
    "type": "object",
    "properties": {"skills": {"type": "array", "items": {"type": "string"}}},
    "required": ["skills"],
}

REPORT_PROMPT = """You are an AI hiring assistant.

You will receive two text inputs:
1. The JOB DESCRIPTION - a technical job description.
2. The RESUME - the resume of an applicant.

===== JOB DESCRIPTION =====
{jd}
===== END OF JOB DESCRIPTION =====

===== RESUME =====
{resume}
===== END OF RESUME =====

A keyword check has already compared the job description's skills with the resume:
Skills found in the resume: {matched}
Skills not found in the resume: {missing}

Your task is to:
- Compare the resume against the Job Description.
- Highlight which required skills and experience match.
- Point out missing or unmatched skills/experience.

Rules:
- Under Skills Matched, list only the skills from "Skills found in the resume". Under Skills Missing, list only the skills from "Skills not found in the resume". Write None if a list is empty.
- Base every statement only on the two documents above. Do not invent experience.
- Do not write a fit rating. It is added automatically after your answer.

Output Format:
- Wherever a line break is needed, use <br> instead of \\n. Use <br> for a single line break and <br><br> for a double line break and so on and so forth.
- Embed all headings between <strong> and </strong>.
- Use no HTML tags other than <br> and <strong>.
Use well-structured bullet points with **highlighted skill names**.
Group your output under the following headings with spacing between each:

<strong>📊 Match Summary</strong>

<strong>✅ Skills Matched:</strong>
- [Skill]: [why it matches]

<strong>💼 Experience Matched:</strong>
- [Experience area]: [why it matches]

<strong>❌ Skills Missing:</strong>
- [Missing Skill]: [Reason it's considered missing]

<strong>⚠️ Experience Gaps:</strong>
- [Gap]: [Explanation]

Replace everything in square brackets with real content.
"""

# ---------------------------------------------------------------- text extraction


async def extract_text(upload: UploadFile, label: str) -> str:
    """The 'Extract from PDF' node: return plain text from a PDF (or .txt) upload."""
    data = await upload.read()
    if not data:
        raise HTTPException(400, f"The {label} file is empty.")

    name = (upload.filename or "").lower()
    try:
        if name.endswith(".txt"):
            text = data.decode("utf-8", errors="ignore")
        else:
            reader = PdfReader(BytesIO(data))
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:
        raise HTTPException(
            400, f"Couldn't read the {label}. Upload a text-based PDF or a .txt file."
        ) from exc

    text = text.strip()
    if not text:
        raise HTTPException(
            422, f"No text found in the {label}. Scanned PDFs need OCR before upload."
        )
    return text[:MAX_CHARS]


# ---------------------------------------------------------------- skill matching (plain Python)

_SPECIAL = (("c++", " cpp "), ("c#", " csharp "), (".net", " dotnet "))


def normalize(text: str) -> str:
    """Lowercase, map C++/C#/.NET to plain tokens, drop punctuation, collapse spaces."""
    text = text.lower()
    for raw, repl in _SPECIAL:
        text = text.replace(raw, repl)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def contains_skill(haystack_norm: str, skill: str) -> bool:
    """Whole-word match of a skill in normalized text, tolerant of plurals and 'node js' vs 'nodejs'."""
    needle = normalize(skill)
    if not needle:
        return False
    padded = f" {haystack_norm} "
    variants = {needle}
    if len(needle) > 3 and needle.endswith("s"):
        variants.add(needle[:-1])
    else:
        variants.add(needle + "s")
    if any(f" {v} " in padded for v in variants):
        return True
    # multi-word skills: "node js" should also match "nodejs"
    if " " in needle:
        compact = needle.replace(" ", "")
        return len(compact) >= 5 and compact in haystack_norm.replace(" ", "")
    return False


def match_skills(skills: list[str], resume_text: str) -> tuple[list[str], list[str]]:
    resume_norm = normalize(resume_text)
    matched, missing = [], []
    for skill in skills:
        (matched if contains_skill(resume_norm, skill) else missing).append(skill)
    return matched, missing


# ---------------------------------------------------------------- LLM providers


class LLMError(Exception):
    """Raised with a message that is safe to show in the UI (never contains the API key)."""


class Provider(Protocol):
    name: str  # "ollama" | "groq"
    model: str
    finish_reason: str  # why the last call stopped ("stop", "length", ...), for diagnostics

    async def ensure_ready(self) -> None: ...
    async def json_completion(self, prompt: str, schema: dict) -> str: ...
    async def text_completion(self, prompt: str) -> str: ...
    def stream(self, prompt: str) -> AsyncIterator[str]: ...


class OllamaProvider:
    """Ollama server (default gemma3:1b). The server URL comes from the environment only."""

    name = "ollama"

    def __init__(self, model: str) -> None:
        self.model = model
        self.finish_reason = ""
        self.url = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
        # Ollama's default context window is small and silently truncates long prompts.
        self.num_ctx = int(os.getenv("OLLAMA_NUM_CTX", "8192"))

    async def ensure_ready(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self.url}/api/tags")
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(
                f"Can't reach Ollama at {self.url}. Start it with `ollama serve`."
            ) from exc
        names = {m.get("name", "") for m in resp.json().get("models", [])}
        if self.model not in names and f"{self.model}:latest" not in names:
            raise LLMError(f"Model {self.model} isn't installed. Run `ollama pull {self.model}`.")

    def _payload(self, prompt: str, **extra) -> dict:
        return {"model": self.model, "messages": [{"role": "user", "content": prompt}], **extra}

    async def json_completion(self, prompt: str, schema: dict) -> str:
        payload = self._payload(
            prompt,
            stream=False,
            format=schema,
            options={"temperature": 0, "num_ctx": self.num_ctx},
        )
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5)) as client:
                resp = await client.post(f"{self.url}/api/chat", json=payload)
                resp.raise_for_status()
            return resp.json()["message"]["content"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise LLMError(f"Ollama failed: {exc}") from exc

    async def text_completion(self, prompt: str) -> str:
        payload = self._payload(
            prompt, stream=False, options={"temperature": 0.2, "num_ctx": self.num_ctx}
        )
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=5)) as client:
                resp = await client.post(f"{self.url}/api/chat", json=payload)
                resp.raise_for_status()
            data = resp.json()
            self.finish_reason = data.get("done_reason", "")
            return data["message"]["content"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise LLMError(f"Ollama failed: {exc}") from exc

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        payload = self._payload(
            prompt, stream=True, options={"temperature": 0.2, "num_ctx": self.num_ctx}
        )
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=5)) as client:
                async with client.stream("POST", f"{self.url}/api/chat", json=payload) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode("utf-8", errors="ignore")
                        raise LLMError(f"Ollama returned {resp.status_code}: {body}")
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        chunk = json.loads(line)
                        if "error" in chunk:
                            raise LLMError(str(chunk["error"]))
                        yield chunk.get("message", {}).get("content", "")
                        if chunk.get("done"):
                            break
        except httpx.HTTPError as exc:
            raise LLMError(f"Lost connection to Ollama: {exc}") from exc


class GroqProvider:
    """Groq's OpenAI-compatible API, using the API key the user typed into the page."""

    name = "groq"

    def __init__(self, model: str, api_key: str) -> None:
        self.model = model
        self.finish_reason = ""
        self._api_key = api_key
        self.base_url = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")

    async def ensure_ready(self) -> None:
        return None  # key and model are validated in build_provider; Groq checks them on first call

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    @staticmethod
    def _error(status: int, body: str) -> str:
        if status == 401:
            return "Groq rejected the API key (401). Check the key you entered."
        try:
            message = json.loads(body)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = body[:200]
        return f"Groq returned {status}: {message}"

    def _payload(self, prompt: str, **extra) -> dict:
        return {"model": self.model, "messages": [{"role": "user", "content": prompt}], **extra}

    async def _complete(self, payload: dict) -> str:
        """One non-streaming chat completion; returns the answer text."""
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10)) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions", headers=self._headers, json=payload
                )
            if resp.status_code != 200:
                raise LLMError(self._error(resp.status_code, resp.text))
            choice = resp.json()["choices"][0]
            self.finish_reason = choice.get("finish_reason") or ""
            return choice["message"].get("content") or ""
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"Groq request failed: {type(exc).__name__}") from exc

    async def json_completion(self, prompt: str, schema: dict) -> str:
        # JSON mode works on every Groq model (the prompt must mention JSON, which ours does).
        return await self._complete(
            self._payload(prompt, temperature=0, response_format={"type": "json_object"})
        )

    async def text_completion(self, prompt: str) -> str:
        return await self._complete(self._payload(prompt, temperature=0.2))

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        payload = self._payload(prompt, temperature=0.2, stream=True)
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10)) as client:
                async with client.stream(
                    "POST", f"{self.base_url}/chat/completions", headers=self._headers, json=payload
                ) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode("utf-8", errors="ignore")
                        raise LLMError(self._error(resp.status_code, body))
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        chunk = json.loads(data)
                        if "error" in chunk:
                            raise LLMError(str(chunk["error"].get("message", chunk["error"])))
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        if choices[0].get("finish_reason"):
                            self.finish_reason = choices[0]["finish_reason"]
                        # Reasoning models put their thinking in a separate field; only the
                        # answer text in "content" is streamed to the page.
                        delta = choices[0].get("delta", {}).get("content")
                        if delta:
                            yield delta
        except httpx.HTTPError as exc:
            raise LLMError(f"Lost connection to Groq: {type(exc).__name__}") from exc


def build_provider(name: str, model: str, api_key: str) -> Provider:
    """Validate what the page sent and build the provider for this one request."""
    name = (name or DEFAULT_PROVIDER).strip().lower()
    if name not in ENABLED_PROVIDERS:
        raise LLMError(f"The '{name}' provider isn't available on this server.")

    model = (model or "").strip() or DEFAULT_MODELS.get(name, "")
    if not model:
        raise LLMError(f"Enter a model name for {name.capitalize()}.")
    if not MODEL_NAME_RE.fullmatch(model):
        raise LLMError("That model name has characters a model name can't contain.")

    if name == "groq":
        api_key = (api_key or "").strip()
        if not api_key:
            raise LLMError("Enter your Groq API key.")
        if not API_KEY_RE.fullmatch(api_key):
            raise LLMError("That doesn't look like a valid API key.")
        return GroqProvider(model, api_key)
    return OllamaProvider(model)


# ---------------------------------------------------------------- pipeline


async def extract_required_skills(provider: Provider, jd_text: str) -> list[str]:
    """Step 1: ask the model for the JD's skills as JSON, then keep only ones really in the JD."""
    prompt = SKILLS_PROMPT.format(jd=jd_text, max_skills=MAX_SKILLS)
    content = await provider.json_completion(prompt, SKILLS_SCHEMA)
    try:
        raw_skills = json.loads(content).get("skills", [])
    except (ValueError, AttributeError):
        raw_skills = []

    jd_norm = normalize(jd_text)
    skills, seen = [], set()
    for item in raw_skills:
        skill = str(item).strip()
        key = normalize(skill)
        if not key or key in seen or len(skill) > 40:
            continue
        if not contains_skill(jd_norm, skill):  # guard against invented skills
            continue
        seen.add(key)
        skills.append(skill)
    return skills[:MAX_SKILLS]


class ThinkFilter:
    """Drops <think>...</think> blocks from a token stream (some reasoning models inline them)."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self.buf = ""
        self.inside = False

    def feed(self, text: str) -> str:
        self.buf += text
        out = []
        while True:
            tag = self.CLOSE if self.inside else self.OPEN
            i = self.buf.find(tag)
            if i >= 0:
                if not self.inside:
                    out.append(self.buf[:i])
                self.buf = self.buf[i + len(tag):]
                self.inside = not self.inside
                continue
            # no complete tag yet: hold back a possible partial tag at the end of the buffer
            keep = 0
            for n in range(min(len(tag) - 1, len(self.buf)), 0, -1):
                if tag.startswith(self.buf[-n:]):
                    keep = n
                    break
            if not self.inside:
                out.append(self.buf[: len(self.buf) - keep])
            self.buf = self.buf[len(self.buf) - keep:]
            return "".join(out)

    def flush(self) -> str:
        rest = "" if self.inside else self.buf
        self.buf = ""
        return rest


def strip_think(text: str) -> str:
    return re.sub(r"<think>.*?(</think>|$)", "", text, flags=re.S)


async def stream_report(provider: Provider, prompt: str, rating_block: str):
    """Step 3: stream the model's explanation, then append the Python-computed rating."""
    think = ThinkFilter()
    chars = 0
    try:
        async for token in provider.stream(prompt):
            text = think.feed(token)
            if text:
                chars += len(text.strip())
                yield text
        tail = think.flush()
        if tail:
            chars += len(tail.strip())
            yield tail

        if chars == 0:
            # The stream carried no answer text. Retry once without streaming so the user still
            # gets a report, and if that is empty too, say so instead of showing only a rating.
            log.warning("empty stream from %s/%s (finish=%s), retrying without streaming",
                        provider.name, provider.model, provider.finish_reason)
            text = strip_think(await provider.text_completion(prompt)).strip()
            if not text:
                raise LLMError(
                    "The model returned no answer text "
                    f"(finish reason: {provider.finish_reason or 'unknown'}). "
                    "If it is a reasoning model, try a non-reasoning model."
                )
            chars = len(text)
            yield text
    except LLMError as exc:
        yield f"\n[[ERROR]] {exc}"
        return
    log.info("report done: provider=%s model=%s chars=%d finish=%s",
             provider.name, provider.model, chars, provider.finish_reason)
    yield rating_block


def build_rating_block(matched: list[str], total: int) -> str:
    if total == 0:
        return (
            "\n\n<strong>🎯 Final Fit Rating:</strong>\n"
            "Not available: no specific skills could be read from the job description.\n"
        )
    pct = round(100 * len(matched) / total)
    return (
        "\n\n<strong>🎯 Final Fit Rating:</strong>\n"
        f"{pct}% ({len(matched)} of {total} required skills found in the resume)\n"
    )


# ---------------------------------------------------------------- routes


@app.post("/api/match")
async def match(
    jd: UploadFile = File(...),
    resume: UploadFile = File(...),
    provider: str = Form(""),
    model: str = Form(""),
    api_key: str = Form(""),
):
    try:
        llm = build_provider(provider, model, api_key)
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc

    jd_text = await extract_text(jd, "job description")
    resume_text = await extract_text(resume, "resume")

    try:
        await llm.ensure_ready()
    except LLMError as exc:
        raise HTTPException(503, str(exc)) from exc
    try:
        skills = await extract_required_skills(llm, jd_text)
    except LLMError as exc:
        raise HTTPException(502, str(exc)) from exc

    matched, missing = match_skills(skills, resume_text)
    prompt = REPORT_PROMPT.format(
        jd=jd_text,
        resume=resume_text,
        matched=", ".join(matched) or "None",
        missing=", ".join(missing) or "None",
    )
    return StreamingResponse(
        stream_report(llm, prompt, build_rating_block(matched, len(skills))),
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/config")
async def config():
    """Defaults for the settings box. Never includes any key."""
    return {
        "providers": ENABLED_PROVIDERS,
        "provider": DEFAULT_PROVIDER,
        "models": DEFAULT_MODELS,
    }


@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "index.html")