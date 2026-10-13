"""Reports which local dependencies are genuinely available. Does not test Gmail/e-signature."""
import shutil
import sys

sys.path.insert(0, ".")
from app.config import Settings  # noqa: E402
from app.llm import OllamaClient  # noqa: E402

s = Settings.from_env()
print("tesseract:", shutil.which("tesseract") or "NOT FOUND (sudo apt install tesseract-ocr)")
print("ollama   :", "reachable with model " + s.llm_model if OllamaClient(s.ollama_url, s.llm_model).available()
      else f"NOT available (model {s.llm_model}); agent will use deterministic fallback")
print("gmail    :", s.gmail_mode, "(TEST adapter)" if s.gmail_mode != "google" else "(real adapter: unverified until tested)")
print("signing  :", s.signing_mode, "(test double only; no real e-signature provider is wired in)")
