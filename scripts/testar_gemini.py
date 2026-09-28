"""Diagnóstico da IA: quais modelos Gemini respondem agora para a sua chave?

Uso:  python scripts/testar_gemini.py
Lê GEMINI_API_KEY do .env. Não imprime a chave.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from central.config import get_settings  # noqa: E402

s = get_settings()
if not s.gemini_api_key or s.gemini_api_key.startswith("<"):
    sys.exit("GEMINI_API_KEY não está preenchida no .env")
headers = {"x-goog-api-key": s.gemini_api_key}
base = "https://generativelanguage.googleapis.com/v1beta"

r = httpx.get(f"{base}/models", headers=headers, params={"pageSize": 200}, timeout=30)
if r.status_code != 200:
    sys.exit(f"Não foi possível listar modelos: HTTP {r.status_code} {r.text[:200]}")
names = [m["name"].split("/")[1] for m in r.json().get("models", [])
         if "generateContent" in m.get("supportedGenerationMethods", []) and "flash" in m["name"]]
print(f"Modelos 'flash' disponíveis para a sua chave: {len(names)}")

configurados = [s.gemini_model] + [m.strip() for m in s.gemini_fallback_model.split(",") if m.strip()]
testar = list(dict.fromkeys(configurados + [n for n in names if "preview" not in n and "tts" not in n
                                            and "image" not in n and "live" not in n and "audio" not in n]))
body = {"contents": [{"role": "user", "parts": [{"text": 'Responda só com o JSON {"ok": true}'}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
print("\nTeste rápido (uma chamada pequena por modelo):")
for name in testar:
    t = time.time()
    try:
        resp = httpx.post(f"{base}/models/{name}:generateContent", json=body, headers=headers, timeout=30)
        status = f"HTTP {resp.status_code}"
    except httpx.HTTPError as e:
        status = f"falha de rede ({type(e).__name__})"
    marca = " <- configurado" if name in configurados else ""
    print(f"  {name:32s} {status:22s} {time.time() - t:5.1f}s{marca}")
print("\nUse no .env os que responderam HTTP 200: GEMINI_MODEL=<melhor> e GEMINI_FALLBACK_MODEL=<outros, separados por vírgula>")
