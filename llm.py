"""Direct OpenAI Chat Completions client (stdlib only, no SDK).
Used by the NCI chat assistant and by message tracking. Set OPENAI_API_KEY (and optionally OPENAI_MODEL, OPENAI_BASE_URL) in .env.
Any OpenAI-compatible endpoint works through OPENAI_BASE_URL. Without a key everything falls back to NCI's rule-based answers."""
import os, json, urllib.request, urllib.error

TRANSPORT = None  # tests inject a fake: fn(url, body, headers) -> parsed JSON response

def key(): return (os.environ.get("OPENAI_API_KEY") or "").strip()
def ready(): return bool(key() or TRANSPORT) and (os.environ.get("NCI_CHAT_LLM") or "1") != "0"
def model(): return (os.environ.get("OPENAI_MODEL") or "gpt-4o-mini").strip()
def base(): return (os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip().rstrip("/")

def chat(messages, max_tokens=700, json_mode=False, timeout=45):
    """-> (text, usage). Raises RuntimeError with the API's message (never the key) on failure."""
    body = {"model": model(), "messages": messages, "max_completion_tokens": max_tokens}
    if json_mode: body["response_format"] = {"type": "json_object"}
    url = base() + "/chat/completions"; hdr = {"Authorization": "Bearer " + key(), "Content-Type": "application/json"}
    if TRANSPORT: r = TRANSPORT(url, body, hdr)
    else:
        try: r = json.load(urllib.request.urlopen(urllib.request.Request(url, json.dumps(body).encode(), hdr), timeout=timeout))
        except urllib.error.HTTPError as e:
            try: msg = json.load(e).get("error", {}).get("message", "")
            except Exception: msg = ""
            raise RuntimeError("OpenAI HTTP %d: %s" % (e.code, msg[:200]))
        except Exception as e: raise RuntimeError("OpenAI unreachable: %s" % type(e).__name__)
    try: text = (r["choices"][0]["message"].get("content") or "").strip()
    except Exception: raise RuntimeError("OpenAI returned no answer")
    return text, r.get("usage") or {}
