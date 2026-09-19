"""Calculate physical item contents from semantic packaging observations."""

SCHEMA = {'anyOf': [{'type':'null'}, {'type':'object', 'additionalProperties':False,
    'properties': {
        'outer_unit': {'type':'string','enum':['case','pack','carton','box','each']},
        'groups': {'type':'integer','minimum':1,'maximum':1000000},
        'each_per_group': {'type':'integer','minimum':1,'maximum':1000000},
        'evidence': {'type':'string','minLength':1,'maxLength':400}},
    'required':['outer_unit','groups','each_per_group','evidence']}]}

INSTRUCTIONS = '''pack_contents interprets physical unit counts regardless of label
layout, abbreviations, or language. Preserve the visible wording in evidence.
For a case of ten packs with fifty tubes per pack: outer_unit=case, groups=10,
each_per_group=50. For a pack of two dozen tips: pack, groups=1, each_per_group=24.
For a box explicitly containing 12 individual bottles: box, 1, 12. Distinguish
the photographed outer unit from its inner packs. Do NOT convert a bottle's
volume (500 mL), weight, dimensions, concentration, or catalog digits into an
item count. A 500mL bottle is one bottle, not 500 each. Unknown contents=null.
Compute no totals yourself: groups and each_per_group are the factors supported
by the source. Do not use ordered, shipped, or received counts as pack contents.
'''


def total(contents, unit):
    if not contents or contents.get('outer_unit') != unit or not contents.get('evidence'):
        return None
    a,b = contents.get('groups'),contents.get('each_per_group')
    if any(type(n) is not int or not 1 <= n <= 1000000 for n in (a,b)):
        return None
    value=a*b
    return value if value <= 1000000 else None


def canonical(contents):
    if not contents:return None
    unit=contents.get('outer_unit');value=total(contents,unit)
    return f'{unit.title()} of {value}' if value else None
