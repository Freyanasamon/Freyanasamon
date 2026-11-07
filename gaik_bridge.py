"""Slow path: GAIK KnowledgeCurator reads the Teams thread and returns fact units with verbatim-verified quotes.
server.apply_units() binds each unit to entities, time and trust state. gaik is imported lazily, so NCI runs without it."""
import os

RULES = ("The source is a Teams thread; every line starts with [message id] author (role). Extract only what people actually said. "
         "A workaround is a POSITIVE observation that something worked; statements that something was NOT tested or NOT validated belong to validation_gap. "
         "Name the node and firmware a statement applies to when the text does. Do not generalise beyond the stated scope.")
SECTIONS = {
    "workaround": ("Workarounds", "Operational workarounds people observed working, what they waited for or did, and the exact node and firmware.", ["scope the workaround was validated on"]),
    "access": ("Access constraints", "Physical or procedural access constraints for a site, gate, cabinet or room, with the date they took effect.", []),
    "hypothesis": ("Hypotheses", "Competing explanations of a fault that are not confirmed, with who proposed them.", []),
    "practice": ("Expert know-how", "Tacit field knowledge a person shares from experience: rules of thumb, things to always or never do, site quirks, with the device or site it concerns.", []),
    "validation_gap": ("Validation gaps", "Things explicitly stated as NOT tested, NOT validated or not applicable on a version or node.", [])}

def installed():
    try:
        import gaik.software_components.knowledge_curator  # noqa: F401
        return True
    except Exception:
        return False

def config():
    from gaik.software_components.llm import get_llm_config
    return get_llm_config(os.environ.get("NCI_LLM_PROVIDER") or None)  # ValueError if the provider key is missing

def available():
    try: return installed() and bool(config())
    except Exception: return False

_Q = str.maketrans({"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"'})
def _n(t): return t.translate(_Q)  # LLMs often swap straight and curly quotes; compare on the normalised form only

def curate(msgs, curator=None):
    """-> (units, conflicts, missing, usage). A unit survives only if its quote is verbatim inside ONE chat message,
    so author, role and time always come from the chat, never from the model."""
    from gaik.software_components.knowledge_curator import KnowledgeCurator, SectionSpec, quote_in_text
    from gaik.software_components.source_normalizer.models import NormalizedSources
    text = "\n".join("[%s] %s (%s): %s" % (m["id"], m["author"], m["role"], m["text"]) for m in msgs)
    src = NormalizedSources.from_texts({"teams-thread.txt": text}, source_class="primary")
    specs = [SectionSpec(id=k, title=v[0], instructions=v[1], required_items=v[2]) for k, v in SECTIONS.items()]
    kb = (curator or KnowledgeCurator(config(), model=os.environ.get("NCI_MODEL") or None)).curate(src, specs, instructions=RULES)
    units, dropped = [], 0
    for sec in kb.sections:
        for u in sec.units:
            mid = next((m["id"] for m in msgs if quote_in_text(_n(u.quote), _n(m["text"]))), None)
            if mid: units.append(dict(kind=sec.section_id, id=mid, summary=u.summary, quote=u.quote, time=u.time_qualifier, conf=u.confidence))
            else: dropped += 1
    return (units, [c.model_dump() for s in kb.sections for c in s.conflicts], [x for s in kb.sections for x in s.missing],
            dict(dict(kb.usage), dropped=dropped + len(kb.dropped)))
