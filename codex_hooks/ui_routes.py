import re


def tokens(code):
    if not isinstance(code, str) or len(code) > 100000:
        return []
    out, i, stack = [], 0, []
    while i < len(code):
        c = code[i]
        if c.isspace():
            i += 1
            continue
        if code.startswith('//', i):
            end = code.find('\n', i)
            i = len(code) if end < 0 else end + 1
            continue
        if code.startswith('/*', i):
            end = code.find('*/', i + 2)
            if end < 0:
                return []
            i = end + 2
            continue
        if c == '/' and (not out or out[-1][1] in {'=', '(', '[', ',', ':', 'return', '!'}):
            i += 1
            in_class = False
            while i < len(code):
                if code[i] == '\\':
                    i += 2
                    continue
                if code[i] == '[':
                    in_class = True
                elif code[i] == ']':
                    in_class = False
                elif code[i] == '/' and not in_class:
                    i += 1
                    while i < len(code) and code[i].isalpha():
                        i += 1
                    break
                i += 1
            else:
                return []
            out.append(('regex', '', len(stack)))
            continue
        if c in "'\"`":
            quote, value = c, ''
            i += 1
            while i < len(code) and code[i] != quote:
                if code[i] == '\\':
                    i += 1
                    if i >= len(code):
                        return []
                    escaped = code[i]
                    if escaped == 'u' and re.fullmatch('[0-9a-fA-F]{4}', code[i + 1:i + 5]):
                        value += chr(int(code[i + 1:i + 5], 16))
                        i += 5
                        continue
                    value += {'n': '\n', 'r': '\r', 't': '\t'}.get(escaped, escaped)
                else:
                    value += code[i]
                i += 1
            if i >= len(code):
                return []
            out.append(('string' if quote != '`' or '${' not in value else 'template', value, len(stack)))
            i += 1
            continue
        match = re.match(r'[A-Za-z_$][\w$]*', code[i:])
        if match:
            value = match.group()
            out.append(('word', value, len(stack)))
            i += len(value)
            continue
        if c in ')]}':
            if not stack or stack.pop() != {')': '(', ']': '[', '}': '{'}[c]:
                return []
        out.append(('punct', c, len(stack)))
        if c in '([{':
            stack.append(c)
            if len(stack) > 32:
                return []
        i += 1
    return out if not stack else []


def awaited_calls(program):
    """Only an await at program depth zero followed directly by a dotted call is supported."""
    ts = tokens(program)
    for i, token in enumerate(ts):
        if token != ('word', 'await', 0):
            continue
        start = i - 1
        while start >= 0 and ts[start][1] != ';':
            start -= 1
        prefix = [t[1] for t in ts[start + 1:i]]
        assignment = r'(?:(?:const|let|var) )?[A-Za-z_$][\w$]*(?: \. [A-Za-z_$][\w$]*)* ='
        if prefix and not re.fullmatch(assignment, ' '.join(prefix)):
            continue
        j, parts = i + 1, []
        while j < len(ts) and ts[j][0] == 'word':
            parts.append(ts[j][1])
            j += 1
            if j < len(ts) and ts[j][1] == '.':
                j += 1
            else:
                break
        if j >= len(ts) or ts[j][1] != '(':
            continue
        depth, end = ts[j][2], j + 1
        while end < len(ts) and not (ts[end][1] == ')' and ts[end][2] == depth):
            end += 1
        if end < len(ts):
            yield parts, ts[j + 1:end]


def programs(name, data):
    code = data.get('code', '')
    if re.fullmatch(r'mcp__cua_repl(?:__|\.)js', name):
        return [('cua', code)]
    if name != 'functions.exec':
        return []
    found = []
    known = {'mcp__cua_repl__js': 'cua', 'mcp__node_repl__js': 'sky'}
    for parts, args in awaited_calls(code):
        if len(parts) != 2 or parts[0] != 'tools' or parts[1] not in known:
            continue
        if not args or args[0][1] != '{':
            continue
        for i in range(1, len(args) - 2):
            if (args[i][1] == 'code' and args[i][2] == 2
                    and args[i - 1][1] in {'{', ','} and args[i + 1][1] == ':'
                    and args[i + 2][0] == 'string'
                    and i + 3 < len(args) and args[i + 3][1] in {',', '}'}):
                found.append((known[parts[1]], args[i + 2][1]))
    return found[:8]


def detect(name, data):
    observations = []
    sky_methods = {'list_apps', 'get_window', 'activate_window', 'get_window_state',
                   'click', 'type_text', 'press_key', 'scroll', 'drag', 'move'}
    cua_methods = {'click', 'type', 'press', 'scroll', 'getState', 'getSnapshot'}
    inspection = {'getApp', 'getState', 'getSnapshot', 'list_apps', 'get_window', 'get_window_state'}
    for route, code in programs(name, data):
        calls = list(awaited_calls(code))
        app_targets = [bool(re.search(r'(?:solidworks|sldworks)', args[0][1], re.I))
                       for parts, args in calls if parts == ['cua', 'getApp']
                       and args and args[0][0] == 'string']
        if route == 'cua' and app_targets and not any(app_targets):
            continue
        for parts, args in calls:
            target, action = None, None
            if route == 'cua':
                if parts == ['cua', 'getApp']:
                    if not args or args[0][0] != 'string':
                        continue
                    target = bool(re.search(r'(?:solidworks|sldworks)', args[0][1], re.I))
                    action = 'getApp'
                elif len(parts) >= 2 and parts[-1] in cua_methods and parts[0] not in {'tools', 'nodeRepl', 'Promise'}:
                    action = parts[-1]
            elif len(parts) == 2 and parts[0] == 'sky' and parts[1] in sky_methods:
                action = parts[1]
                for i in range(1, len(args) - 2):
                    if (args[i][1] == 'app' and args[i][2] == 2 and args[i - 1][1] in {'{', ','}
                            and args[i + 1][1] == ':' and args[i + 2][0] == 'string'):
                        target = bool(re.search(r'(?:solidworks|sldworks)', args[i + 2][1], re.I))
            if not action or target is False:
                continue
            observed = {'route': route, 'operation': action,
                'kind': 'inspection' if action in inspection else 'unknown',
                'attribution': 'confirmed' if target else 'candidate',
                'reason': 'Supported executable UI route; scope requires assessment.' if not target else
                          'Current executable call identifies SolidWorks; capability coverage requires assessment.'}
            if observed not in observations:
                observations.append(observed)
    return observations[:32]
