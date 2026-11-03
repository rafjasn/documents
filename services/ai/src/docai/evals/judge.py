"""LLM judge for answers the scoring rules cannot decide."""

from docai.evals.golden import GoldenItem
from docai.evals.llm import Messages, Models, Reply

RULES = {
    "number": "The candidate is correct if the value it gives as its answer equals the reference, in the same currency or unit; leaving the currency or unit out is fine. A value it says the answer is not, or one it only uses on the way to its answer, does not count. It is incorrect if it gives a different value as the answer, or two values that contradict each other.",
    "text": "The candidate is correct if it states the same fact as the reference, in any language or wording. Extra detail is fine; a different or missing fact is incorrect.",
    "date": "The candidate is correct if it gives the same date as the reference, in any format or language.",
    "boolean": "The reference is a yes/no answer. The candidate is correct if it reaches the same conclusion, even without saying yes or no explicitly.",
    "refusal": "The documents do not contain this information, so the candidate must not give a value for it. It is correct if it gives no value: saying the information is not stated or not provided, that there is none (for example that a contract provides no discount), that it cannot be found, or asking for the document all count as correct. It is incorrect if it states a value, guesses one, or presents other information as the answer.",
}

PROMPT = """You grade answers to questions about business documents.

Question: {question}
Reference answer: {reference}
Candidate answer: {answer}

{rule}
Reply with exactly one word: CORRECT or INCORRECT."""


def judge_messages(item: GoldenItem, answer: str) -> Messages:
    reference = item.answer
    if item.accepted and len(item.accepted) > 1:
        reference += f" (also acceptable: {'; '.join(item.accepted[1:])})"
    prompt = PROMPT.format(
        question=item.question,
        reference=reference,
        answer=answer.strip(),
        rule=RULES[item.kind],
    )
    return [("human", prompt)]


def verdict(text: str) -> bool | None:
    upper = text.upper()
    if "INCORRECT" in upper:
        return False
    if "CORRECT" in upper:
        return True
    return None


def judge(
    models: Models, spec: str, item: GoldenItem, answer: str, run_name: str
) -> tuple[bool, Reply]:
    reply = models.call(
        spec,
        judge_messages(item, answer),
        run_name=f"{run_name}-judge",
        metadata={"item_id": item.id},
    )
    decided = verdict(reply.text)
    if decided is None:
        raise ValueError(f"{item.id}: judge reply has no verdict: {reply.text[:200]!r}")
    return decided, reply
