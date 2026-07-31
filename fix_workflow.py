import json

BACKSLASH = chr(92)

def to_js_esc(text):
    """Replace non-ASCII chars with JS unicode escapes like Т"""
    out = []
    for ch in text:
        cp = ord(ch)
        if cp > 127:
            out.append(BACKSLASH + 'u' + format(cp, '04x'))
        else:
            out.append(ch)
    return ''.join(out)

with open('D:/Job/workflow_live.json', encoding='utf-8') as f:
    wf = json.load(f)

for node in wf['nodes']:
    params = node.get('parameters', {})
    if 'jsCode' in params:
        params['jsCode'] = to_js_esc(params['jsCode'])

payload = {
    'name': wf['name'],
    'nodes': wf['nodes'],
    'connections': wf['connections'],
    'settings': wf['settings'],
    'staticData': wf.get('staticData'),
}

with open('D:/Job/wf_patch.json', 'w', encoding='ascii') as f:
    json.dump(payload, f, ensure_ascii=True)

# Verify
with open('D:/Job/wf_patch.json', 'rb') as f:
    content = f.read()

idx = content.find(b'prompt')
print('Prompt snippet:', content[idx:idx+80])

# Check Т (correct T) vs Р (mojibake R)
u0422_js = b'\\u0422'
u0420_js = b'\\u0420'
print()
print('\\u0422 (T correct):', content.count(u0422_js), 'times')
print('\\u0420 (R mojibake):', content.count(u0420_js), 'times')
