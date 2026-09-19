"""Conservative recognition of explicit shortage captions, not stock inference."""

import re


def is_delivery_item(item):
    assessment = item.get('receipt_assessment') or {}
    return (assessment.get('kind') == 'delivery' and assessment.get('confidence') == 'high'
            and bool((assessment.get('evidence') or '').strip())
            and item.get('label_type') != 'unrelated')


def has_arrival_statement(caption):
    """Positive arrival evidence may accompany the reason for ordering supplies."""
    for clause in re.split(r'[.!?;\n]|\bbut\b', caption.casefold()):
        arrival = re.search(r'\b(arriv(?:e|es|ed)|delivered|received|fetched|picked up)\b', clause)
        if not arrival:
            continue
        before = clause[:arrival.start()]
        if re.search(r"\b(no|not|never|haven't|hasn't|hadn't|wasn't|weren't|isn't|aren't|didn't|don't|doesn't|will|would|should|might|may|expect(?:ed|ing)?|waiting|when|whether|if)\b", before):
            continue
        if re.search(r'\b(has|have|did)\s+(?:it|they|we|you|the\b.*?)\s*$', before):
            continue  # A question is not arrival evidence.
        return True
    return False


def is_shortage_report(caption):
    text = (caption or '').casefold().replace('\u2019', "'")
    shortage = re.search(
        r'\b(running\s+(?:out|low)|runs?\s+out|ran\s+out|out\s+of|used\s+up|used\s+all|'
        r'low\s+on|need\s+(?:some\s+)?more|need\s+to\s+(?:re)?order|please\s+(?:re)?order|'
        r'(?:buy|order)\s+more|very\s+few|almost\s+(?:empty|gone)|empty\s+(?:bottle|box|container)|'
        r'(?:bottle|box|container)\s+(?:is\s+)?empty|only\b[^.!?\n]{0,80}\bleft|'
        r'(?:none|nothing|no\b[^.!?\n]{0,50})\s+left)\b', text)
    return bool(shortage) and not has_arrival_statement(text)


def legacy_shortage(record, result):
    """Preserve exclusions for old cached results; new semantic reads bypass this.

    Earlier versions could label known shortage photos as deliveries. Removing
    the old intake veto must not retroactively import those historical results.
    """
    return result.get('semantic_assessment_version', 0) < 4 and is_shortage_report(record.get('caption', ''))
