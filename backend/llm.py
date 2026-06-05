import os
import json
import re
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, AIMessage

from logger import get_logger

load_dotenv()

log = get_logger(__name__)

_anthropic_client = None
_openai_client = None


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _anthropic_client


def _get_openai_client():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI
        _openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _openai_client


def _get_context(member_id: str) -> str:
    from legislation_meta import get_meta
    m = get_meta(member_id)
    if m and m.get("context"):
        return m["context"]
    return f"the legislative record of council member {member_id}."


SYSTEM_PROMPT_TEMPLATE = """You are a helpful assistant for {context}

Your job is to help residents understand this councilmember's legislative record, policy positions, and actions — explained in plain language, like talking to a neighbor, not a lawyer.

Rules:
1. Base your answer ONLY on the document excerpts provided. Do not use outside knowledge.
2. Cite each point inline as a markdown link with this exact format: [Short Title](source_label) — for example [Food Resources Motion](25-0381/filename.pdf). The link text must be a 2-4 word Title-Case description of the document's topic. The href must be ONLY the source label (e.g. "25-0381/filename.pdf") — no dollar amounts, no extra words, no spaces. Never put anything other than the source label in the href.
3. If the excerpts come from multiple council files, cite each piece of information with its own inline link.
4. If the documents don't have enough to fully answer the question:
   - Say clearly: "I don't know based on these documents."
   - Suggest which type of council file might have the answer.
5. Always end your response with exactly 3 suggested follow-up questions the user could ask — questions that CAN be answered from these documents.
6. Keep the answer concise. Default to 2 short paragraphs. Only when the user's question is broad or detailed may you use up to 3 paragraphs — never more than 3.

Format your response as JSON with this exact structure:
{{
  "answer": "your plain-language answer here, with inline [2-4 word title](council_file_id/filename.pdf) links",
  "sources": [{{"title": "Food Resources Motion", "source": "council_file_id/filename.pdf"}}],
  "followups": ["Question 1?", "Question 2?", "Question 3?"]
}}

Each source's "title" must match the inline link text used for that document and stay 2-4 words reflecting its topic. Only return the JSON — no extra text before or after it."""


def _system_prompt(member_ids: list[str]) -> str:
    context = _get_context(member_ids[0]) if member_ids else "LA City Council legislation."
    return SYSTEM_PROMPT_TEMPLATE.format(context=context)


def _build_context(chunks: list[dict]) -> str:
    parts = [f"[Source: {c['source']}]\n{c['text']}" for c in chunks]
    return "\n\n---\n\n".join(parts)


def _parse_llm_output(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning(
            "Failed to parse LLM output as JSON (%s); falling back to raw text — "
            "followups will be empty and may appear inline. Raw length: %d chars. Preview: %r",
            e, len(raw), raw[:300],
        )
        return {"answer": raw, "sources": [], "followups": []}


def _format_history_for_openai(prior_messages: list) -> str:
    if not prior_messages:
        return ""
    lines = ["Prior conversation:"]
    for msg in prior_messages:
        role = "User" if isinstance(msg, HumanMessage) else "Assistant"
        lines.append(f"{role}: {msg.content}")
    return "\n".join(lines) + "\n\n"


def _call_claude(
    question: str,
    chunks: list[dict],
    member_ids: list[str],
    prior_messages: list | None = None,
) -> dict:
    import anthropic
    prior_messages = prior_messages or []
    has_history = bool(prior_messages)
    log.info("Calling Claude (claude-haiku-4-5) for %s%s",
             member_ids, f" (history: {len(prior_messages)//2} turns)" if has_history else "")

    context = _build_context(chunks)
    approx_words = len(context.split()) + len(question.split())
    log.info("Prompt size: ~%d words", approx_words)

    messages = []
    for msg in prior_messages:
        role = "user" if isinstance(msg, HumanMessage) else "assistant"
        messages.append({"role": role, "content": str(msg.content)})
    messages.append({
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": f"Document excerpts:\n\n{context}",
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": f"\n\nQuestion: {question}",
            },
        ],
    })

    client = _get_anthropic_client()
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": _system_prompt(member_ids),
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=messages,
    )
    raw = response.content[0].text
    cache_read = response.usage.cache_read_input_tokens or 0
    cache_written = response.usage.cache_creation_input_tokens or 0
    log.info("Claude responded (%d chars). Tokens — input: %d, cache_hit: %d, cache_written: %d, output: %d",
             len(raw), response.usage.input_tokens, cache_read, cache_written, response.usage.output_tokens)

    result = _parse_llm_output(raw)
    if not result.get("sources"):
        log.warning("No sources returned in response")
    return result


def _call_openai(
    question: str,
    chunks: list[dict],
    member_ids: list[str],
    prior_messages: list | None = None,
) -> dict:
    prior_messages = prior_messages or []
    log.info("Calling OpenAI (gpt-4o-mini) for %s", member_ids)
    context = _build_context(chunks)
    history_preamble = _format_history_for_openai(prior_messages)
    user_message = f"{history_preamble}Document excerpts:\n\n{context}\n\nQuestion: {question}"
    log.info("Prompt size: ~%d words", len(user_message.split()))

    client = _get_openai_client()
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": _system_prompt(member_ids)},
            {"role": "user", "content": user_message},
        ],
        max_tokens=1024,
    )
    raw = response.choices[0].message.content
    log.info("OpenAI responded (%d chars)", len(raw))
    result = _parse_llm_output(raw)
    if not result.get("sources"): 
        log.warning("No sources returned in response")
    return result


def contextualize_question(question: str, prior_messages: list) -> str:
    """Rewrite a follow-up into a standalone retrieval query using chat history.

    Follow-ups like "say more" or "what documents is this from?" carry no topic
    on their own, so embedding them retrieves irrelevant chunks. Rewriting them
    against the conversation restores the topic. Returns the original question if
    there's no history or on any error (so retrieval still proceeds)."""
    if not prior_messages:
        return question

    history_lines = []
    for msg in prior_messages:
        role = "User" if isinstance(msg, HumanMessage) else "Assistant"
        history_lines.append(f"{role}: {msg.content}")
    user_text = (
        "Conversation so far:\n" + "\n".join(history_lines) + "\n\n"
        f"Follow-up message: {question}\n\n"
        "Rewrite the follow-up as a standalone search query for retrieving "
        "documents. Resolve pronouns and references using the conversation. If "
        "the follow-up asks about sources or the previous answer, target the "
        "topic of that previous answer. Return ONLY the query text."
    )

    provider = os.getenv("LLM_PROVIDER", "claude").lower()
    try:
        if provider == "openai":
            client = _get_openai_client()
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": user_text}],
                max_tokens=128,
            )
            rewritten = resp.choices[0].message.content
        else:
            client = _get_anthropic_client()
            resp = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=128,
                messages=[{"role": "user", "content": user_text}],
            )
            rewritten = resp.content[0].text
    except Exception as e:
        log.warning("Query contextualization failed (%s); using original question", e)
        return question

    rewritten = (rewritten or "").strip().strip('"')
    if not rewritten:
        return question
    log.info("Contextualized query: %r -> %r", question, rewritten)
    return rewritten


def get_response(
    question: str,
    chunks: list[dict],
    member_ids: list[str],
    session_id: str | None = None,
) -> dict:
    from history import load_recent

    prior_messages = load_recent(session_id) if session_id else []

    provider = os.getenv("LLM_PROVIDER", "claude").lower()
    log.info("Provider: %s", provider)
    if provider == "openai":
        result = _call_openai(question, chunks, member_ids, prior_messages)
    else:
        result = _call_claude(question, chunks, member_ids, prior_messages)

    return result
