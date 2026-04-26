import re
import math

def _smart_float_fmt(x, base_dec):
    """Formater float med base_dec desimaler; øker automatisk for tall < 1."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return str(x)
    if x != x:   # NaN
        return ''
    ax = abs(x)
    if ax == 0:
        return f'{x:.{base_dec}f}'
    if ax >= 1e15 or (0 < ax < 1e-9):
        return f'{x:.{base_dec}e}'
    if 0 < ax < 1:
        # Antall ledende nuller etter komma → ekstra desimaler for signifikante siffer
        leading_zeros = max(0, -math.floor(math.log10(ax)) - 1)
        dec = base_dec + leading_zeros
    else:
        dec = base_dec
    return f'{x:.{dec}f}'

class MicroParser:
    def __init__(self):
        # Regex for 'aggregate' mønster: (stat) var -> ny_var
        self.agg_pattern = re.compile(r"\((?P<stat>\w+)\)\s+(?P<src>[\w@/]+)(?:\s*->\s*(?P<target>\w+))?")
        
        # Regex for import: register-var [time] [as name] og import-event: register-var time to time [as name]
        # Støtter norske tegn i alias (fødselsdato m.m.)
        self.import_pattern = re.compile(
            r"(?P<var>[\w/]+)"
            r"(?:\s+(?P<date1>\d{4}-\d{2}-\d{2}))?"
            r"(?:\s+to\s+(?P<date2>\d{4}-\d{2}-\d{2}))?"
            r"(?:\s+as\s+(?P<alias>[\wøæåØÆÅ]+))?",
            re.UNICODE
        )
        self.date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    def _parse_agg_spec(self, text: str) -> list:
        """Parse Stata-style aggregation spec: (stat) var1 var2 -> tgt (stat2) var3 ...
        A (stat) applies to all following variables until the next (stat) token."""
        # Tokenise: find (stat) groups and var[->target] items
        token_re = re.compile(
            r"\((?P<stat>\w+)\)"                             # (stat)
            r"|(?P<src>[\w@/]+)(?:\s*->\s*(?P<target>\w+))?"  # var [-> target]
        )
        targets = []
        current_stat = None
        for m in token_re.finditer(text):
            if m.group('stat'):
                current_stat = m.group('stat')
            elif m.group('src') and current_stat:
                targets.append({
                    'stat': current_stat,
                    'src': m.group('src'),
                    'target': m.group('target'),
                })
        return targets

    def preprocess_script(self, script_text):
        """Preprosessering av script: håndterer for-each og linjefortsettelse med backslash."""

        # 1) Ekspander alle 'for-each'-løkker slik at motoren kan kjøre lineært.
        while True:
            loop_match = re.search(r"for-each\s+(\w+)\s+in\s+([^{]+)\s*\{([^}]+)\}", script_text, re.DOTALL)
            if not loop_match:
                break
            var_name = loop_match.group(1)
            items = loop_match.group(2).strip().split()
            body = loop_match.group(3).strip()
            expanded = ""
            for item in items:
                expanded += body.replace(var_name, item) + "\n"
            script_text = script_text[:loop_match.start()] + expanded + script_text[loop_match.end():]

        # 2) Linjefortsettelse ala microdata: backslash på slutten av linjen
        #    betyr at neste linje skal henge sammen med denne.
        lines = script_text.splitlines()
        combined = []
        buffer = ""
        for raw in lines:
            line = raw.rstrip()
            if line.endswith("\\"):
                # Fjern trailing backslash og legg til et mellomrom som separator
                part = line[:-1].rstrip()
                buffer += (part + " ")
            else:
                if buffer:
                    buffer += line
                    combined.append(buffer)
                    buffer = ""
                else:
                    combined.append(line)
        if buffer:
            combined.append(buffer)

        return "\n".join(combined)

    def parse_line(self, line):
        line = line.strip()
        if not line or line.startswith('//'):
            return None
        # Strip inline // comments (outside string literals)
        in_str = None
        for i, ch in enumerate(line):
            if ch in ('"', "'"):
                if in_str is None:
                    in_str = ch
                elif in_str == ch:
                    in_str = None
            elif ch == '/' and i + 1 < len(line) and line[i + 1] == '/' and in_str is None:
                line = line[:i].rstrip()
                break
        if not line:
            return None

        # 1. Skill ut opsjoner (alt etter første komma), men IKKE for kommandoer der komma
        #    kan forekomme i argumenter/etiketter (generate, recode, define-labels, …).
        #    NB: Naiv split(',') knekker f.eks. recode … (1/3 = 1 "Jordbruk, skogbruk, fiske")
        #    ved første komma INNE i anførselstegn — da blir resten av reglene borte.
        # keep/drop/replace: betingelse kan ha komma (f.eks. inrange(alder, 16, 66))
        _no_comma_option_split = frozenset({
            'generate', 'recode', 'define-labels', 'keep', 'drop', 'replace',
        })
        options_dict = {}
        first_word = line.split(maxsplit=1)[0].lower() if line else ''
        if ',' in line and first_word not in _no_comma_option_split:
            line, opt_part = line.split(',', 1)
            # Finner opsjoner som 'robust' eller 'by(kommune)'
            opt_matches = re.finditer(r"(?P<opt>\w+)(?:\((?P<arg>[^)]+)\))?", opt_part)
            for m in opt_matches:
                arg = m.group('arg')
                options_dict[m.group('opt')] = arg.strip() if arg else True

        # 2. Skill ut 'if'-betingelse
        condition = None
        if ' if ' in line:
            line, condition = line.split(' if ', 1)

        # 3. Kommando og argumenter
        parts = line.split(maxsplit=1)
        command = parts[0].lower()
        remainder = parts[1] if len(parts) > 1 else ""
        # Tillat barchart(mean) uten mellomrom: flytt (stat...) til starten av remainder
        _m_inline = re.match(r'^(\w+)(\(\w.*)', command)
        if _m_inline:
            command = _m_inline.group(1)
            remainder = _m_inline.group(2) + (' ' + remainder if remainder else '')

        args = self._parse_command_logic(command, remainder)

        return {
            "command": command,
            "args": args,
            "condition": condition.strip() if condition else None,
            "options": options_dict
        }

    def _parse_command_logic(self, cmd, remainder):
        if cmd == 'aggregate':
            targets = self._parse_agg_spec(remainder)
            return {"targets": targets}
        if cmd == 'collapse':
            targets = self._parse_agg_spec(remainder)
            return {"targets": targets}

        if cmd in ('ivregress', 'ivregress-predict'):
            # Støtter: [method] depvar [exog...] (endog... = instr...)
            # method (2sls/liml/gmm) er valgfri og default 2sls
            m_paren = re.search(r'\(([^=)]+)=([^)]+)\)', remainder)
            if not m_paren:
                return {"raw": remainder}
            before = remainder[:m_paren.start()].split()
            _method_tokens = {'2sls', 'liml', 'gmm'}
            method = '2sls'
            dep_var = None
            exog = []
            for i, tok in enumerate(before):
                if tok.lower() in _method_tokens:
                    method = tok.lower()
                elif dep_var is None:
                    dep_var = tok
                else:
                    exog.append(tok)
            endog = [t.strip() for t in m_paren.group(1).split() if t.strip()]
            instruments = [t.strip() for t in m_paren.group(2).split() if t.strip()]
            return {'dep': dep_var, 'exog': exog, 'endog': endog,
                    'instruments': instruments, 'method': method}

        if cmd == 'rdd':
            # rdd depvar runvar [covariates...]
            toks = remainder.split()
            if len(toks) < 2:
                return {"raw": remainder}
            return {'dep': toks[0], 'runvar': toks[1], 'exog': toks[2:]}

        if cmd == 'merge':
            # Ny syntaks: merge var-list into dataset [on variable]
            m = re.match(
                r"^(.*?)\binto\b\s+(\w+)(?:\s+on\s+(\w+))?\s*$",
                remainder.strip(), re.IGNORECASE
            )
            if m:
                vars_part = m.group(1).strip().split()
                return {'vars': vars_part, 'into': m.group(2), 'on': m.group(3)}
            # Gammel syntaks: merge datasett-navn [, on(nøkkel)]
            toks = remainder.strip().split()
            return toks if toks else []

        if cmd in ['import', 'import-event']:
            match = self.import_pattern.search(remainder)
            return match.groupdict() if match else {"raw": remainder}
        if cmd == 'import-panel':
            # import-panel var1 var2 ... time1 time2 ...
            toks = remainder.split()
            vars_list, dates_list = [], []
            for t in toks:
                if self.date_pattern.match(t):
                    dates_list.append(t)
                else:
                    vars_list.append(t)
            return {"vars": vars_list, "dates": dates_list} if vars_list else {"raw": remainder}
            
        if cmd == 'generate':
            if '=' in remainder:
                target, expr = remainder.split('=', 1)
                return {"target": target.strip(), "expression": expr.strip()}

        if cmd == 'rename':
            parts = remainder.split()
            return {"old": parts[0], "new": parts[1]} if len(parts) >= 2 else {"raw": remainder}

        if cmd == 'replace':
            if '=' in remainder:
                target, expr = remainder.split('=', 1)
                return {"target": target.strip(), "expression": expr.strip()}
            return {"raw": remainder}

        if cmd == 'drop':
            if remainder.strip().lower().startswith('if '):
                return {"mode": "if", "condition": remainder[3:].strip()}
            return {"mode": "vars", "vars": remainder.split()}

        if cmd == 'keep':
            if remainder.strip().lower().startswith('if '):
                return {"mode": "if", "condition": remainder[3:].strip()}
            return {"mode": "vars", "vars": remainder.split()}

        if cmd == 'clone-variables':
            # var1 -> new1 var2 -> new2  eller  var1 var2 (da new = var_clone)
            pairs = []
            rest = remainder
            for m in re.finditer(r"([\wøæåØÆÅ]+)\s*(?:->\s*([\wøæåØÆÅ]+))?", rest, re.UNICODE):
                v, n = m.group(1), m.group(2)
                if v and v not in ('prefix', 'suffix'):
                    pairs.append((v, n if n else v + '_clone'))
            return {"pairs": pairs}

        if cmd == 'destring':
            return {"vars": remainder.split()}

        if cmd == 'recode':
            # var1 var2 (1 2 3 = 0) (4 = 1)  -- skill vars fra rules
            rule_pos = remainder.find('(')
            if rule_pos >= 0:
                vars_part = remainder[:rule_pos].strip().split()
                rules_part = remainder[rule_pos:]
                rules = re.findall(r'\(([^)]+)\)', rules_part)
                return {"vars": vars_part, "rules": rules}
            return {"vars": remainder.split(), "rules": []}

        if cmd == 'define-labels':
            return self._parse_define_labels(remainder)
        if cmd == 'assign-labels':
            parts = remainder.split()
            return {"var": parts[0], "codelist": parts[1]} if len(parts) >= 2 else {"raw": remainder}
        if cmd == 'drop-labels':
            return {"names": remainder.split()}
        if cmd == 'list-labels':
            # codelist-name | register-var [time]
            toks = remainder.split()
            return {"codelist": toks[0], "time": toks[1] if len(toks) > 1 and self.date_pattern.match(toks[1]) else None}

        if cmd in ['reshape-to-panel', 'reshape-from-panel']:
            return {"prefixes": remainder.split()} if cmd == 'reshape-to-panel' else {}

        if cmd == 'require':
            # require <source> as <alias> – no-op for kompatibilitet, vi kobler ikke til SSB
            m = re.match(r"(.+?)\s+as\s+(\w+)\s*$", remainder.strip())
            return {"source": m.group(1).strip(), "alias": m.group(2)} if m else {}
        if cmd == 'delete-dataset':
            toks = remainder.split()
            return [toks[0]] if toks else {"raw": remainder}
        if cmd == 'rename-dataset':
            toks = remainder.split()
            return [toks[0], toks[1]] if len(toks) >= 2 else {"raw": remainder}

        if cmd == 'textblock':
            return {}
        if cmd == 'endblock':
            return {}

        if cmd == 'let':
            if '=' not in remainder:
                return {"raw": remainder}
            name, expr = remainder.split('=', 1)
            return {"name": name.strip(), "expression": expr.strip()}

        if cmd == 'for':
            # for var in val1 val2 ... | for var in start : end
            if ' in ' not in remainder:
                return {"raw": remainder}
            var_part, in_part = remainder.split(' in ', 1)
            var_name = var_part.strip()
            spec = in_part.strip()
            # Range-syntaks: "lo : hi" eller "lo:hi" (med/uten mellomrom)
            m_range = re.match(r'^\s*(-?\d+)\s*:\s*(-?\d+)\s*$', spec)
            if m_range:
                try:
                    lo, hi = int(m_range.group(1)), int(m_range.group(2))
                    return {"var": var_name, "values": list(range(lo, hi + 1))}
                except ValueError:
                    pass
            vals = [t.strip() for t in spec.split()]
            result = []
            for v in vals:
                try:
                    result.append(int(v) if '.' not in v else float(v))
                except ValueError:
                    result.append(v)
            return {"var": var_name, "values": result}

        if cmd == 'end':
            return {}  # Avslutter for-løkke

        if cmd == 'sample':
            # sample count|fraction seed
            toks = remainder.split()
            if len(toks) < 2:
                return {"raw": remainder}
            try:
                first = float(toks[0])
                seed_val = int(toks[1])
                if first >= 1 and first == int(first):
                    return {"count": int(first), "seed": seed_val}
                if 0 < first < 1:
                    return {"fraction": first, "seed": seed_val}
            except (ValueError, TypeError):
                pass
            return {"raw": remainder}

        # Figurkommandoer: barchart (stat) var [var...], histogram var, boxplot var
        if cmd == 'barchart':
            m = re.match(r"\(\s*(\w+)\s*\)\s*(.+)", remainder.strip())
            if m:
                stat, rest = m.group(1).lower(), m.group(2).strip()
                return {"stat": stat, "vars": rest.split()}
            return {"stat": "count", "vars": remainder.split()} if remainder.strip() else {"raw": remainder}
        if cmd == 'histogram':
            return {"vars": remainder.split()} if remainder.strip() else {"raw": remainder}
        if cmd == 'boxplot':
            return {"vars": remainder.split()} if remainder.strip() else {"raw": remainder}
        if cmd == 'scatter':
            toks = remainder.split()
            return {"vars": toks} if len(toks) >= 2 else {"raw": remainder}
        if cmd == 'piechart':
            m = re.match(r"\(\s*(\w+)\s*\)\s*(.+)", remainder.strip())
            if m:
                stat, rest = m.group(1).lower(), m.group(2).strip()
                return {"stat": stat, "vars": rest.split()}
            return {"stat": "count", "vars": remainder.split()} if remainder.strip() else {"raw": remainder}
        if cmd == 'hexbin':
            toks = remainder.split()
            return {"vars": toks} if len(toks) >= 2 else {"raw": remainder}
        if cmd == 'sankey':
            toks = remainder.split()
            return {"vars": toks} if len(toks) >= 2 else {"raw": remainder}

        if cmd == 'coefplot':
            # coefplot reg-cmd dep-var var1 var2 ...
            toks = remainder.split()
            if not toks:
                return {"raw": remainder}
            return {"reg_cmd": toks[0].lower(), "vars": toks[1:]}

        # Overlevelsesanalyse: cox hendelse tid [var1 var2...], kaplan-meier hendelse tid, weibull hendelse tid
        if cmd == 'cox':
            toks = remainder.split()
            return toks if len(toks) >= 2 else {"raw": remainder}  # event, duration (covariater valgfrie)
        if cmd in ['kaplan-meier', 'kaplan_meier', 'weibull']:
            toks = remainder.split()
            return toks if len(toks) >= 2 else {"raw": remainder}  # event, duration

        return remainder.split()

    def _tokenize_quoted(self, s):
        """Tokeniser streng med respekt for '...' og \"...\"."""
        tokens = []
        i = 0
        while i < len(s):
            while i < len(s) and s[i].isspace():
                i += 1
            if i >= len(s):
                break
            if s[i] in "\"'":
                quote = s[i]
                i += 1
                start = i
                while i < len(s) and s[i] != quote:
                    i += 1
                tokens.append(s[start:i])
                i += 1  # skip closing quote
            else:
                start = i
                while i < len(s) and not s[i].isspace() and s[i] not in "\"'":
                    i += 1
                tokens.append(s[start:i])
        return tokens

    def _parse_define_labels(self, remainder):
        """Parse define-labels: codelist-name value label [value label ...]"""
        tokens = self._tokenize_quoted(remainder.strip())
        if len(tokens) < 3 or len(tokens) % 2 == 0:
            return {"raw": remainder}
        name = tokens[0]
        pairs = []
        for i in range(1, len(tokens), 2):
            val_str, label = tokens[i], tokens[i + 1]
            try:
                val = int(val_str) if val_str.lstrip('-').isdigit() else float(val_str)
            except (ValueError, TypeError):
                val = val_str
            pairs.append((val, label))
        return {"name": name, "pairs": pairs}


class LabelManager:
    """Håndterer define-labels, assign-labels, drop-labels, list-labels."""

    def __init__(self, catalog=None):
        self.codelists = {}  # codelist_name -> {value: label}
        self.var_to_codelist = {}  # var_name -> codelist_name
        self._load_from_catalog(catalog or {})

    def _load_from_catalog(self, catalog):
        """Pre-last kodelister fra variable_metadata (labels eller codelist per variabel)."""
        for var_path, meta in catalog.items():
            if isinstance(meta, dict) and 'labels' in meta:
                # Bruk kortnavn som codelist-navn
                short = var_path.split('/')[-1] + '_labels'
                self.codelists[short] = {self._conv(k): v for k, v in meta['labels'].items()}
            if isinstance(meta, dict) and 'codelist' in meta and meta['codelist'] not in self.codelists:
                # Referanse til codelist – må først være definert
                pass

    def _conv(self, v):
        try:
            return int(v) if str(v).lstrip('-').isdigit() else float(v)
        except (ValueError, TypeError):
            return v

    def define_labels(self, name, pairs):
        """define-labels codelist-name value label [value label ...]"""
        self.codelists[name] = {self._conv(v): label for v, label in pairs}

    def assign_labels(self, var_name, codelist_name):
        """assign-labels var-name codelist-name"""
        if codelist_name not in self.codelists:
            raise ValueError(f"Kodeliste '{codelist_name}' finnes ikke. Bruk define-labels først.")
        self.var_to_codelist[var_name] = codelist_name

    def drop_labels(self, names):
        """drop-labels codelist-name [codelist-name ...]"""
        for n in names:
            self.codelists.pop(n, None)
            self.var_to_codelist = {v: c for v, c in self.var_to_codelist.items() if c != n}

    def get_codelist_for_var(self, var_name, time=None):
        """Returnerer codelist-dict for variabel, eller None."""
        return self.codelists.get(self.var_to_codelist.get(var_name))

    def format_value(self, var_name, value):
        """Returnerer label for verdi hvis codelist finnes, ellers råverdi."""
        cl = self.get_codelist_for_var(var_name)
        if cl is None:
            return value
        # Prøv både eksakt type og konvertert (1 vs 1.0)
        if value in cl:
            return cl[value]
        if pd.isna(value):
            return value
        try:
            v2 = int(value) if isinstance(value, float) and value == int(value) else value
            return cl.get(v2, value)
        except (ValueError, TypeError):
            return cl.get(value, value)

    def list_labels_output(self, codelist_or_var, time=None, catalog=None):
        """Formater kodeliste for list-labels. codelist_or_var er navn eller variabel."""
        if codelist_or_var in self.codelists:
            cl = self.codelists[codelist_or_var]
        else:
            cname = self.var_to_codelist.get(codelist_or_var)
            if cname:
                cl = self.codelists.get(cname)
            else:
                # Prøv register-var i katalog
                short = codelist_or_var.split('/')[-1] if '/' in codelist_or_var else codelist_or_var
                meta = (catalog or {}).get(codelist_or_var) or next(
                    (v for k, v in (catalog or {}).items() if k.endswith('/' + short)), {}
                )
                if isinstance(meta, dict) and meta.get('labels'):
                    cl = {self._conv(k): v for k, v in meta['labels'].items()}
                else:
                    return f"Kodeliste eller variabel '{codelist_or_var}' ikke funnet."
        if not cl:
            return f"Kodeliste '{codelist_or_var}' er tom."
        lines = [f"\n--- Kodeliste: {codelist_or_var} ---"]
        for val, label in sorted(cl.items(), key=lambda x: (str(x[0]), x[1])):
            lines.append(f"  {val}  {label}")
        return "\n".join(lines)

    def apply_to_tabulate_result(self, result, var1, var2=None):
        """Mapp rad/kolonneindeks gjennom codelist for tabulate-output."""
        if result is None:
            return result
        lm = self
        if hasattr(result, 'index'):
            cl1 = lm.get_codelist_for_var(var1)
            if cl1:
                new_idx = result.index.map(lambda x: lm.format_value(var1, x) if not (isinstance(x, tuple) and len(x) > 1) else x)
                if not (isinstance(result.index[0], tuple) if len(result.index) else False):
                    result = result.copy()
                    result.index = new_idx
                else:
                    # Flere variabler i rad – mapp første
                    def map_row(r):
                        if isinstance(r, tuple):
                            return (lm.format_value(var1, r[0]),) + r[1:]
                        return lm.format_value(var1, r)
                    result.index = result.index.map(map_row)
        if var2 and hasattr(result, 'columns') and hasattr(result.columns, 'map'):
            cl2 = lm.get_codelist_for_var(var2)
            if cl2:
                result.columns = result.columns.map(lambda x: lm.format_value(var2, x))
        return result


import pandas as pd
import numpy as np
import hashlib
def _eval_int(x):
    """Element-wise int for generate expressions when functions.py is not available."""
    if hasattr(x, 'astype'):
        return np.trunc(x).astype(int)
    return int(x)


try:
    from functions import get_microdata_functions, set_label_manager, set_bindings
    _EVAL_LOCALS = {**get_microdata_functions(), 'np': np}
    _LET_EVAL_ENV = {k: v for k, v in _EVAL_LOCALS.items()}
    _LET_EVAL_ENV['__builtins__'] = {}
except ImportError:
    set_label_manager = lambda lm: None
    set_bindings = lambda b: None
    _EVAL_LOCALS = {
        'np': np,
        'int': _eval_int,
        'float': lambda x: np.asarray(x, dtype=float) if hasattr(x, '__len__') and not isinstance(x, str) else float(x),
        'round': np.round,
    }
    _LET_EVAL_ENV = {'__builtins__': {}, 'abs': abs, 'round': round, 'min': min, 'max': max}
import json
from pathlib import Path


def _normalize_distribution_weights(weight_dict):
    """Konverter vekter (positive tall) til sannsynligheter som summerer til 1. Tillater at metadata bruker vekter i stedet for strengt sum=1."""
    if not weight_dict:
        return [], []
    keys = list(weight_dict.keys())
    vals = [float(weight_dict[k]) for k in keys]
    total = sum(vals)
    if total <= 0:
        return keys, [1.0 / len(keys)] * len(keys)
    probs = [v / total for v in vals]
    return keys, probs


def _split_top_level_bool(s, sep):
    """
    Del streng s på tegn sep ('&' eller '|') som er utenfor parenteser og utenfor anførselstegn.
    """
    if sep not in ('&', '|'):
        raise ValueError("sep må være '&' eller '|'")
    parts = []
    depth = 0
    quote = None
    i = 0
    start = 0
    n = len(s)
    while i < n:
        c = s[i]
        if quote:
            if c == '\\' and i + 1 < n:
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "'\"":
            quote = c
            i += 1
            continue
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
        elif c == sep and depth == 0:
            parts.append(s[start:i])
            start = i + 1
        i += 1
    parts.append(s[start:])
    return parts


def _strip_outer_parens(s):
    """Fjern én ytre parentespar som omslutter hele uttrykket (streng- og dybdebevisst)."""
    s = s.strip()
    if len(s) < 2 or s[0] != '(' or s[-1] != ')':
        return s
    depth = 0
    quote = None
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if quote:
            if c == '\\' and i + 1 < n:
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "'\"":
            quote = c
            i += 1
            continue
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                if i != n - 1:
                    return s
                return s[1:-1].strip()
        i += 1
    return s


def _stata_like_bool_fixup(expr):
    """
    Stata-lignende presedens for & og | før Python eval: | ytterst, deretter &.
    Pakker inn ledd slik at == ikke bindes feil mot bitwise & (Python).
    """
    if not isinstance(expr, str):
        return expr
    s = expr.strip()
    if not s:
        return s
    while True:
        t = _strip_outer_parens(s)
        if t == s:
            break
        s = t
    or_parts = [p.strip() for p in _split_top_level_bool(s, '|') if p.strip()]
    if len(or_parts) > 1:
        return ' | '.join(f'({_stata_like_bool_fixup(p)})' for p in or_parts)
    and_parts = [p.strip() for p in _split_top_level_bool(s, '&') if p.strip()]
    if len(and_parts) > 1:
        return ' & '.join(f'({_stata_like_bool_fixup(p)})' for p in and_parts)
    return s


def _micro_expr_fixup(expr):
    """Oversett microdata-syntaks til gyldig Python:
    - ! → ~ (negasjon), men bevar !=
    - Fjern ledende nuller i heltall: date(2010,01,01) → date(2010,1,1)
    - Enslig . (manglende verdi) → np.nan
    """
    if not isinstance(expr, str):
        return expr
    # Steg 0: Erstatt enslige '.' (microdata missing-verdi) med np.nan.
    # Må ikke ødelegge desimaltall (3.14, .5), attributt-tilgang (df.col),
    # eller metodekall. Matcher kun '.' som ikke grenser til ord-tegn eller andre punktum.
    if '.' in expr:
        expr = re.sub(r'(?<![\w.])\.(?![\w.])', 'np.nan', expr)
    # Steg 1: fjern ledende nuller utenfor strenger (f.eks. 01 → 1, 007 → 7)
    # Matcher komma/parentes + 0-prefiks + siffer(e), men ikke 0 alene eller 0.noe
    if '0' in expr:
        fixed = []
        j = 0
        m = len(expr)
        q = None
        while j < m:
            ch = expr[j]
            if q:
                fixed.append(ch)
                if ch == '\\' and j + 1 < m:
                    j += 1
                    fixed.append(expr[j])
                elif ch == q:
                    q = None
                j += 1
                continue
            if ch in "'\"":
                q = ch
                fixed.append(ch)
                j += 1
                continue
            # Etter ( eller , og mellomrom: fjern ledende nuller fra heltall
            if ch == '0' and j > 0:
                prev_non_ws = j - 1
                while prev_non_ws >= 0 and expr[prev_non_ws] == ' ':
                    prev_non_ws -= 1
                if prev_non_ws >= 0 and expr[prev_non_ws] in '(,':
                    # Sjekk at neste tegn er et siffer (ikke . eller slutt)
                    k = j + 1
                    while k < m and expr[k] == '0':
                        k += 1
                    if k < m and expr[k].isdigit():
                        # Hopp over ledende nuller, behold siste signifikante
                        j = k
                        continue
            fixed.append(ch)
            j += 1
        expr = ''.join(fixed)

    # Steg 1.5: .astype(int) / dtype=int → bruk "int64"-streng, fordi `int`
    # i eval-miljøet er rebundet til int_-funksjonen (elementvis trunc) og
    # ikke kan brukes som dtype av pandas/numpy.
    if '.astype(int' in expr or 'dtype=int' in expr:
        expr = re.sub(r'\.astype\(\s*int\s*(?=[,)])', '.astype("int64"', expr)
        expr = re.sub(r'\bdtype\s*=\s*int\s*(?=[,)])', 'dtype="int64"', expr)

    if '!' not in expr:
        return expr
    # Steg 2: ! → ~ (men bevar !=)
    out = []
    i = 0
    n = len(expr)
    quote = None
    while i < n:
        c = expr[i]
        if quote:
            out.append(c)
            if c == '\\' and i + 1 < n:
                i += 1
                out.append(expr[i])
            elif c == quote:
                quote = None
            i += 1
            continue
        if c in "'\"":
            quote = c
            out.append(c)
            i += 1
            continue
        if c == '!' and i + 1 < n and expr[i + 1] == '=':
            out.append('!=')
            i += 2
            continue
        if c == '!':
            out.append('~')
            i += 1
            continue
        out.append(c)
        i += 1
    return ''.join(out)


def _py_eval_expr(df, expr):
    """
    Evaluer et microdata-uttrykk med ren Python eval:
    - Kolonnenavn i df blir variabler (Series) i eval-miljøet.
    - get_microdata_functions() + np ligger også i miljøet.
    - Resultatet normaliseres til en Series med samme index som df.
    """
    if expr is None or (isinstance(expr, str) and not expr.strip()):
        raise ValueError("Tomt uttrykk i generate.")
    expr = _micro_expr_fixup(expr)
    # Bygg eval-miljø: kolonnenavn -> Series, microdata-funksjoner og np
    env = dict(_EVAL_LOCALS)
    # Kolonnenavn med @ (f.eks. date@panel) er ugyldige Python-identifikatorer.
    # Erstatt @ med _AT_ i både env-nøkler og uttrykket.
    at_cols = {}
    for col in df.columns:
        if '@' in col:
            safe = col.replace('@', '_AT_')
            at_cols[col] = safe
            env[safe] = df[col]
        else:
            env[col] = df[col]
    if at_cols:
        for orig, safe in at_cols.items():
            expr = expr.replace(orig, safe)
    # Kjør ren eval (ingen ekstra sikkerhet nødvendig i dette miljøet)
    result = eval(expr, {}, env)
    if isinstance(result, pd.Series):
        # Sikre riktig index
        if not result.index.equals(df.index):
            result = result.reindex(df.index)
        return result
    # Numpy-array med samme lengde som df
    if hasattr(result, '__len__') and not isinstance(result, (str, bytes)) and len(result) == len(df):
        return pd.Series(result, index=df.index)
    # Skalar: broadcast over alle rader
    return pd.Series(result, index=df.index)


def _py_eval_cond(df, expr):
    """
    Evaluer en betingelse (if-uttrykk) til en boolsk mask (Series[bool]) med samme index som df.
    Uttrykk preprosesseres med Stata-lignende presedens for & og | (| ytterst) slik at ==
    ikke bindes feil mot bitwise & i Python.
    """
    if isinstance(expr, str):
        expr = _stata_like_bool_fixup(expr)
    res = _py_eval_expr(df, expr)
    if not isinstance(res, pd.Series):
        return pd.Series(bool(res), index=df.index)
    if res.dtype != bool:
        return res.astype(bool)
    return res


def _line_condition_mask(df, expr, options):
    """
    Radmaske for keep/drop/replace: bruk _condition_mask fra options når tolkeren har bygget den,
    ellers full Python-eval via _py_eval_cond (støtter inrange, &, | — ikke begrenset som pandas eval).
    """
    if options:
        m = options.get('_condition_mask')
        if m is not None:
            return m
    if not expr:
        return None
    return _py_eval_cond(df, expr)


_DEMO_FALLBACK_META = {
    # Disse brukes kun som sikkerhetsnett når runtime-catalog mangler labels/distribution
    # (f.eks. Pyodide der ekstern metadata ikke lastes inn riktig).
    "kjonn": {
        "type": "register",
        "data_type": "string",
        "microdata_datatype": "Alfanumerisk",
        "labels": {"1": "Mann", "2": "Kvinne"},
        "distribution": {"1": 0.51, "2": 0.49},
    },
    "BEFOLKNING_KJONN": {
        "type": "register",
        "data_type": "string",
        "microdata_datatype": "Alfanumerisk",
        "labels": {"1": "Mann", "2": "Kvinne"},
        "distribution": {"1": 0.51, "2": 0.49},
    },
    "NUDB_BU": {
        "type": "register",
        "data_type": "string",
        "microdata_datatype": "Alfanumerisk",
        "labels": {
            "0": "Ingen utdanning",
            "1": "Barneskole",
            "2": "Ungdomsskole",
            "3": "Videregående",
            "4": "Videregående - avsluttende",
            "5": "Påbygging til videregående",
            "6": "UH-utdanning - lavere nivå",
            "7": "UH-utdanning - høyere nivå",
            "8": "Forskerutdanning",
            "9": "Uoppgitt",
        },
        "distribution": {
            "0": 0.01,
            "1": 0.10,
            "2": 0.12,
            "3": 0.30,
            "4": 0.18,
            "5": 0.10,
            "6": 0.12,
            "7": 0.05,
            "8": 0.01,
            "9": 0.01,
        },
    },
    "REGSYS_VIRK_NACE1_SN07": {
        "type": "register",
        "data_type": "string",
        "microdata_datatype": "Alfanumerisk",
        "labels": {
            "00.000": "Uoppgitt",
            "01.110": "Dyrking av korn (unntatt ris), belgvekster og oljeholdige vekster",
            "03.111": "Hav- og kystfiske",
            "05.100": "Bryting av steinkull",
            "10.710": "Produksjon av brød og ferske konditorvarer",
            "35.111": "Produksjon av elektrisitet fra vannkraft",
            "41.200": "Oppføring av bygninger",
            "43.120": "Grunnarbeid",
            "47.111": "Butikkhandel med bredt vareutvalg med hovedvekt på nærings- og nytelsesmidler",
            "49.410": "Godstransport på vei",
            "55.101": "Drift av hoteller, pensjonater og moteller med restaurant",
            "62.010": "Programmeringstjenester",
            "64.190": "Bankvirksomhet ellers",
            "69.100": "Juridisk tjenesteyting",
            "70.210": "PR og kommunikasjonstjenester",
            "77.110": "Utleie og leasing av biler og andre lette motorvogner",
            "84.110": "Generell offentlig administrasjon",
            "85.201": "Ordinær grunnskoleundervisning",
            "86.211": "Allmenn legetjeneste",
            "96.020": "Frisering og annen skjønnhetspleie",
            "99.000": "Internasjonale organisasjoner og organer",
        },
        "distribution": {
            "00.000": 0.02,
            "01.110": 0.03,
            "03.111": 0.04,
            "05.100": 0.02,
            "10.710": 0.10,
            "35.111": 0.04,
            "41.200": 0.06,
            "43.120": 0.04,
            "47.111": 0.15,
            "49.410": 0.08,
            "55.101": 0.08,
            "62.010": 0.06,
            "64.190": 0.06,
            "69.100": 0.04,
            "70.210": 0.03,
            "77.110": 0.03,
            "84.110": 0.03,
            "85.201": 0.05,
            "86.211": 0.06,
            "96.020": 0.03,
            "99.000": 0.01,
        },
    },
    "REGSYS_ARB_YRKE_STYRK08": {
        "type": "register",
        "data_type": "string",
        "microdata_datatype": "Alfanumerisk",
        "labels": {
            "0000": "Uoppgitt / yrker som ikke kan identifiseres",
            "0110": "Offiserer fra fenrik og høyere grad",
            "1120": "Administrerende direktører",
            "2120": "Matematikere, statistikere mv.",
            "3115": "Maskiningeniører",
            "4131": "Dataregistrere",
            "5131": "Servitører",
            "6121": "Melke- og husdyrprodusenter",
            "7115": "Tømrere og snekkere",
            "8111": "Bergfagarbeidere",
            "9112": "Renholdere i virksomheter",
            "XXXX": "Uoppgitt/ukjent yrke",
        },
        "distribution": {
            "0000": 0.02,
            "0110": 0.01,
            "1120": 0.10,
            "2120": 0.08,
            "3115": 0.08,
            "4131": 0.10,
            "5131": 0.16,
            "6121": 0.04,
            "7115": 0.12,
            "8111": 0.17,
            "9112": 0.12,
            "XXXX": 0.00,
        },
    },
}

# Når BOSATTEFDT_BOSTED / BOSATT_KOMMUNE mangler i runtime-katalog (f.eks. avkortet JSON),
# må FORMELL likevel få reelle kommunekoder — ikke uniform -2..9999 (gir koder uten label).
_MINIMAL_KOMMUNE_BASE = {
    "type": "register",
    "data_type": "string",
    "microdata_datatype": "Alfanumerisk",
    "labels": {
        "0301": "Oslo",
        "1103": "Stavanger",
        "1108": "Sandnes",
        "1508": "Ålesund",
        "1804": "Bodø",
        "3001": "Halden",
        "3107": "Fredrikstad",
        "3203": "Asker",
        "3301": "Drammen",
        "3403": "Hamar",
        "3907": "Sandefjord",
        "4003": "Skien",
        "4204": "Kristiansand",
        "4601": "Bergen",
        "5001": "Trondheim",
        "5501": "Tromsø",
        "5601": "Alta",
    },
    "distribution": {
        "0301": 0.22,
        "4601": 0.12,
        "5001": 0.10,
        "1103": 0.07,
        "4204": 0.06,
        "3107": 0.05,
        "3301": 0.05,
        "4003": 0.04,
        "1508": 0.04,
        "1804": 0.04,
        "5501": 0.04,
        "1108": 0.03,
        "3403": 0.03,
        "3907": 0.03,
        "3203": 0.03,
        "3001": 0.02,
        "5601": 0.02,
    },
}

# Referanseår for alder fra BEFOLKNING_FOEDSELS_AAR_MND (demo-syntese)
_DEMO_REF_YEAR = 2025

# Felles latent faktor per unit_id (N(0,1)): binder lønn og formue i syntetiske data.
# Større koeffisient på formue enn på lønn (formue mer «persistent» i forhold til latent evne).
_NORWAY_LATENT_LOG_WAGE = 0.22
_NORWAY_LATENT_LOG_WEALTH_NET = 0.52
_NORWAY_LATENT_LOG_WEALTH_GROSS = 0.44
_NORWAY_LATENT_LOG_INCOME_OTHER = 0.15
# Stønads-/ytelsesvariabler: lavere sannsynlighet for utbetaling når latent inntektsevne er høy.
_NORWAY_LATENT_TRANSFER_HURDLE_SHIFT = 0.04


def _norway_latent_z(unit_id: int) -> float:
    """Deterministisk standardnormal fra unit_id (samme z for alle variabler på samme person)."""
    h = hashlib.md5(f"norway_latent_v1:{int(unit_id)}".encode()).digest()
    u1 = int.from_bytes(h[:4], "big") / 2**32
    u2 = int.from_bytes(h[4:8], "big") / 2**32
    u1 = max(1e-12, min(1.0 - 1e-12, u1))
    u2 = max(1e-12, min(1.0 - 1e-12, u2))
    return float(np.sqrt(-2.0 * np.log(u1)) * np.cos(2.0 * np.pi * u2))


def _rule_cond_value_equal(cv, v):
    """Like verdier i regel-vilkår (streng '1' vs int 1)."""
    if isinstance(v, list):
        try:
            c = float(cv)
        except (TypeError, ValueError):
            return False
        return v[0] <= c <= v[1]
    try:
        if isinstance(v, bool):
            return bool(cv) == v
        if isinstance(v, (int, float, np.integer, np.floating)):
            return float(cv) == float(v)
    except (TypeError, ValueError):
        pass
    return cv == v


def _norway_demo_unit_seed(unit_id, salt: str) -> int:
    return int(hashlib.md5(f"{salt}:{int(unit_id)}".encode()).hexdigest(), 16) % (2**32)


def _norway_synth_age_from_uid(unit_id) -> int:
    """Deterministisk alder 18–67 (typisk yrkesaktiv) for demo når fødselsdato mangler."""
    r = np.random.default_rng(_norway_demo_unit_seed(unit_id, "alder"))
    a = int(round(r.normal(44.0, 14.0)))
    return max(18, min(67, a))


def _norway_synth_kjonn_from_uid(unit_id) -> int:
    r = np.random.default_rng(_norway_demo_unit_seed(unit_id, "kjonn"))
    return 1 if r.random() < 0.51 else 2


# ── Hierarkisk NUS2000-kodegenerator ──────────────────────────────────────────
# NUS2000 har 6 siffer: nivå (siffer 1), fagfelt (siffer 2-3), spesialisering (4-6).
# Nivå-sannsynligheter (siffer 1: utdanningsnivå)
_NUS_LEVEL_PROBS = {
    0: 0.02,   # Ingen utdanning / førskole
    1: 0.10,   # Barneskole
    2: 0.12,   # Ungdomsskole
    3: 0.25,   # VGS grunnutdanning
    4: 0.20,   # VGS avsluttende
    5: 0.05,   # Påbygging til studiekompetanse
    6: 0.14,   # UH lavere nivå (bachelor)
    7: 0.08,   # UH høyere nivå (master)
    8: 0.03,   # Forskerutdanning
    9: 0.01,   # Uoppgitt
}

# Fagfelt per nivå (siffer 2-3). Fordelt etter omtrentlig norsk utdanningsstatistikk.
_NUS_FIELD_PROBS = {
    0: {99: 1.0},
    1: {1: 0.70, 19: 0.15, 99: 0.15},
    2: {1: 0.60, 11: 0.05, 19: 0.10, 99: 0.25},
    3: {1: 0.15, 11: 0.06, 12: 0.04, 13: 0.04, 14: 0.08,
        15: 0.18, 16: 0.12, 17: 0.04, 18: 0.06, 19: 0.08, 99: 0.15},
    4: {1: 0.10, 11: 0.05, 12: 0.03, 13: 0.03, 14: 0.07,
        15: 0.20, 16: 0.15, 17: 0.05, 18: 0.07, 19: 0.10, 99: 0.15},
    5: {1: 0.30, 15: 0.20, 16: 0.15, 14: 0.10, 99: 0.25},
    6: {1: 0.08, 11: 0.04, 12: 0.10, 13: 0.08, 14: 0.12,
        15: 0.15, 16: 0.18, 17: 0.03, 18: 0.05, 19: 0.07, 99: 0.10},
    7: {1: 0.05, 11: 0.04, 12: 0.08, 13: 0.10, 14: 0.10,
        15: 0.20, 16: 0.15, 17: 0.03, 18: 0.05, 19: 0.10, 99: 0.10},
    8: {1: 0.05, 12: 0.05, 13: 0.10, 14: 0.05,
        15: 0.30, 16: 0.20, 17: 0.05, 19: 0.10, 99: 0.10},
    9: {99: 1.0},
}

# Aldersbetinget justering av nivå-sannsynligheter (multiplikator)
_NUS_LEVEL_AGE_SHIFT = {
    (0, 5):   {0: 5.0},
    (6, 12):  {1: 5.0},
    (13, 15): {2: 5.0},
    (16, 19): {3: 3.0, 4: 2.0},
    (20, 25): {6: 2.5, 5: 1.5, 4: 1.5},
    (26, 35): {6: 2.0, 7: 2.5, 8: 2.0},
    (36, 55): {4: 1.5, 6: 1.3, 7: 1.8},
    (56, 67): {3: 1.5, 4: 1.3},
}


def _generate_nus_code(rng, age=None):
    """Generer én 6-sifret NUS2000-kode hierarkisk, betinget på alder."""
    # 1) Nivå (siffer 1) — juster sannsynligheter etter alder
    probs = dict(_NUS_LEVEL_PROBS)
    if age is not None:
        for (lo, hi), shifts in _NUS_LEVEL_AGE_SHIFT.items():
            if lo <= age <= hi:
                for lev, mult in shifts.items():
                    if lev in probs:
                        probs[lev] *= mult
                break
    levels = list(probs.keys())
    p = np.array([probs[l] for l in levels], dtype=float)
    p /= p.sum()
    level = int(rng.choice(levels, p=p))

    # 2) Fagfelt (siffer 2-3)
    fields_d = _NUS_FIELD_PROBS.get(level, {99: 1.0})
    fkeys = list(fields_d.keys())
    fp = np.array([fields_d[k] for k in fkeys], dtype=float)
    fp /= fp.sum()
    field = int(rng.choice(fkeys, p=fp))

    # 3) Spesialisering (siffer 4-6): 999=uspesifisert er vanligst i data
    if rng.random() < 0.40:
        spec = 999
    else:
        spec = int(rng.integers(1, 200)) * 10 + int(rng.integers(0, 10))
        spec = min(spec, 999)

    return f"{level}{field:02d}{spec:03d}"


def _generate_nus_codes_vec(n, rng, ages=None):
    """Generer n NUS2000-koder, valgfritt betinget på alder-array."""
    result = np.empty(n, dtype=object)
    for i in range(n):
        age = float(ages[i]) if ages is not None else None
        result[i] = _generate_nus_code(rng, age)
    return result


def _norway_classify_money_demo(meta: dict, short_name: str):
    """
    Klassifiser variabel for norsk demo-syntese (kronebeløp).
    Returnerer None hvis standard mean/std (normal) er mer passende.
    """
    sn = (short_name or "").upper()
    # Timer/antall/vekt er ikke kronebeløp — utelukk tidlig.
    if any(x in sn for x in ("_TIMER", "_ANTALL", "_VEKT")):
        return None
    desc = (meta.get("description") or "").lower()
    kw = " ".join(meta.get("keywords") or []).lower()
    blob = f"{sn} {desc} {kw}"
    mx = meta.get("max")
    # Tydelige kodelister / små tall (ikke årsbeløp i kroner)
    if mx is not None and mx <= 100 and "inntekt" not in sn and "skatt" not in sn and "formue" not in sn:
        return None

    # --- 1) Navnebasert (før «max 9999»-sperre) ---
    if "NETTOFORMUE" in sn or "nettoformue" in blob:
        return "wealth_net"
    if (
        "BRUTTOFORM" in sn
        or "BER_BRFORM" in sn
        or "bruttoformue" in blob
        or ("FORMUE_UTLAND" in sn)
        or ("bruttofinans" in blob)
    ):
        return "wealth_gross"
    if "WLONN" in sn or "lønnsinntekt" in blob:
        return "wage_fallback"
    if any(
        x in sn
        for x in (
            "INNTEKT_LONN",
            "INNTEKT_YRKINNT",
            "INNTEKT_WYRKINNT",
            "INNTEKT_NARINNT",
            "INNTEKT_WNARINNT",
            "INNTEKT_WSAMINNT",
        )
    ):
        return "wage_fallback"
    # Lønnsskjema-variabler (ARBLONN_ lønnskomponenter i NOK)
    if any(x in sn for x in (
        "ARBLONN_LONN_FAST",
        "ARBLONN_LONN_KONTANT",
        "ARBLONN_LONN_EKV_FMLONN",
        "ARBLONN_LONN_EKV_IALT",
        "ARBLONN_LONN_EKV_BONUS",
        "ARBLONN_LONN_EKV_UREGTIL",
        "ARBLONN_LONN_EKV_VEKT",
    )):
        return "wage_fallback"
    if any(x in sn for x in (
        "ARBLONN_LONN_FERIE",
        "ARBLONN_LONN_HELLIGDAG",
        "ARBLONN_LONN_ANNEN_BET",
        "ARBLONN_LONN_GODTGJORELSE",
        "ARBLONN_LONN_NATURAL",
        "ARBLONN_LONN_SLUTTVEDERLAG",
        "ARBLONN_LONN_UREGTIL",
    )):
        return "transfer_hurdle"
    if any(x in sn for x in (
        "ARBLONN_LONN_BONUS",
        "ARBLONN_LONN_OVERTID",
        "ARBLONN_LONN_FERIE_TREKK",
    )):
        return "transfer_hurdle"
    if "INNTEKT_HUSH_IES" in sn:
        return "income_generic"
    if any(x in sn for x in ("INNTEKT_OVERFOR", "INNTEKT_SKPLOVF", "INNTEKT_WSKFROVF")):
        return "transfer_hurdle"
    if "INNTEKT_STUDIESTIPEND" in sn:
        return "transfer_hurdle"
    if "INNTEKT_SEK_MARK" in sn or "sekundærbolig" in desc:
        return "wealth_gross"
    if any(x in sn for x in ("SOSHJELP_BIDRAG", "SOSHJELP_LAAN")):
        return "transfer_hurdle"
    # Stønad / syke / foreldre / sosial (mange nuller i et år)
    if any(
        x in sn
        for x in (
            "SYKEPENGER",
            "FORELDREPENGER",
            "SUM_ARBAVKL",
            "INNTEKT_SOSIAL",
            "INNTEKT_FTRYG",
            "GRUNN_HJELP",
            "INNTEKT_ARBLED",
        )
    ):
        return "transfer_hurdle"
    # Pensjon og AFP (ofte null for yrkesaktive)
    if any(
        x in sn
        for x in (
            "ALDERSP",
            "AFP_",
            "TJENPEN",
        )
    ):
        return "pension_hurdle"
    # Pensjonsgivende inntekt ≈ yrkesinntekt i størrelse
    if "PGIVINNT" in sn:
        return "wage_fallback"
    # Barn / bostøtte
    if any(x in sn for x in ("BARNETRYGD", "KONTANTSTOTTE", "BOSTOTTE")):
        return "transfer_child"
    # Gjeld — usikret (forbrukslån) vs. total (inkl. boliglån)
    if "USIKRET_GJELD" in sn:
        return "debt_unsecured"
    if "GJELD" in sn:
        return "debt"
    # Bank, verdipapir, fond (formue — korrelerer med latent)
    if any(x in sn for x in ("BANKINNSK", "VERDIPAPIR", "INNTEKT_FOND", "AKSJEUTBYTTE")):
        return "capital_financial"
    if "BRUTTO_FINANSKAPITAL" in sn:
        return "wealth_gross"
    if "REALISASJONS" in sn:
        return "capital_gain_loss"
    if "BER_REALKAP" in sn:
        return "real_capital_stock"
    if "INNTEKT_REAL" in sn and "REALISASJONS" not in sn:
        return "real_wealth_component"
    if "RENTUT" in sn:
        return "interest_expense"
    if "RENTINNT" in sn:
        return "interest_flow"
    if any(x in sn for x in ("FORMUESKATT", "UTSKATT", "INNTEKT_UTSKATT")):
        return "tax_amount"
    if "yrkesinntekt" in blob or "samlet inntekt" in desc:
        return "income_total"
    if "formue" in blob or "finanskapital" in blob:
        return "wealth_gross"
    if meta.get("mean") is not None and float(meta["mean"]) >= 200_000 and (
        "inntekt" in blob or "skatt" in blob
    ):
        return "income_generic"

    # --- 2) «max 9999» i metadata er ofte teknisk tak — ikke bruk uniform 0–9999 for INNTEKT_/SKATT_ ---
    if mx is not None and mx <= 10000:
        if sn.startswith("INNTEKT_") or sn.startswith("SKATT_"):
            return "income_generic"
        return None
    return None


def _norway_lognormal_kr_rows(
    rng: np.random.Generator,
    log_mu_row: np.ndarray,
    sigma: float,
    as_int: bool = True,
    min_v: float = 0.0,
):
    """Lognormal med én log-middelverdi per rad: exp(log_mu_row + sigma * Z)."""
    log_mu_row = np.asarray(log_mu_row, dtype=float)
    n = len(log_mu_row)
    e = rng.standard_normal(n)
    raw = np.exp(log_mu_row + float(sigma) * e)
    if as_int:
        out = np.round(raw).astype(np.int64)
        if min_v is not None:
            out = np.maximum(out, int(min_v))
        return out
    return np.maximum(raw, float(min_v))


def _norway_lognormal_kr(rng, n_rows: int, log_mu: float, sigma: float, as_int: bool = True, min_v: float = 0.0):
    return _norway_lognormal_kr_rows(rng, np.full(int(n_rows), float(log_mu)), sigma, as_int=as_int, min_v=min_v)


def _norway_demo_ages_from_current_df(current_df):
    """Alder fra BEFOLKNING_FOEDSELS_AAR_MND når den finnes (for avhengig demo-syntese)."""
    if current_df is None or getattr(current_df, "empty", True):
        return None
    if "BEFOLKNING_FOEDSELS_AAR_MND" not in current_df.columns:
        return None
    bym = pd.to_numeric(current_df["BEFOLKNING_FOEDSELS_AAR_MND"], errors="coerce").fillna(198505).astype(np.int64)
    return (_DEMO_REF_YEAR - (bym // 100)).clip(0, 110).astype(float).values


def _norway_wage_age_gender_params(ages, gender, z):
    """Aldersprofil og kjønnsjustering for yrkesinntekter (demo)."""
    a = np.asarray(ages, dtype=float)
    z = np.asarray(z, dtype=float)
    g = np.asarray(gender, dtype=int)
    # p_zero: høy for barn/unge og pensjonister, lav for yrkesaktive
    p0 = np.select(
        [a < 16, a < 18, a < 22, a < 25, a < 67, a < 72, a >= 72],
        [0.97,   0.85,   0.50,   0.22,   0.04,   0.55,   0.82],
        default=0.04,
    )
    # log_mu: median NOK per aldersgruppe (z=0, ingen kjønnsjustering)
    # 25–35: ~360k → 35–50: ~600k (topp) → 50–67: ~530k → 67+: ~100k
    lm = np.select(
        [a < 16, a < 18, a < 22, a < 25, a < 35, a < 50, a < 67, a >= 67],
        [9.5,    10.4,   11.5,   12.2,   12.9,   13.3,   13.15,  11.5],
        default=13.3,
    )
    # Kjønnsjustering: menn ~15 % høyere på log-skala (norsk realitet)
    gender_adj = np.where(g == 1, 0.08, -0.07)
    lm = lm + _NORWAY_LATENT_LOG_WAGE * z + gender_adj
    p0 = np.clip(p0, 0.02, 0.98)
    return p0, lm


def _norway_sykepenger_hurdle_params(ages, z):
    """Aldersprofiler for sykepenger: sannsynlighet for null og log-nivå blant positive (demo)."""
    a = np.asarray(ages, dtype=float)
    z = np.asarray(z, dtype=float)
    # Basis p0 og log_mu per aldersintervall (yrkesaktiv kjerne lavere p0)
    p0 = np.select(
        [
            a < 16,
            a < 18,
            a < 30,
            a < 45,
            a < 55,
            a < 62,
            a < 67,
            a >= 67,
        ],
        [0.97, 0.94, 0.79, 0.74, 0.72, 0.75, 0.82, 0.90],
        default=0.88,
    )
    lm = np.select(
        [
            a < 16,
            a < 18,
            a < 30,
            a < 45,
            a < 55,
            a < 62,
            a < 67,
            a >= 67,
        ],
        [10.15, 10.45, 11.45, 11.85, 12.0, 11.85, 11.55, 10.95],
        default=11.2,
    )
    p0 = np.clip(p0 - _NORWAY_LATENT_TRANSFER_HURDLE_SHIFT * z, 0.42, 0.98)
    lm = lm - 0.12 * z
    return p0, lm


def _norway_sample_categorical(meta, n_rows, rng, ages_vec=None, gender_vec=None, z_vec=None):
    """
    Sample kategoriske koder med alderkondisjonering (age_distribution), kjønn (gender_distribution)
    og latent-z myk vekting (z_shift). Returnerer numpy object-array med strengkoder.

    age_distribution-format støtter valgfrie kjønns-undernøkler:
      - Flat: {"25-54": {"1": 0.80, ...}}
      - Med kjønn: {"25-54": {"male": {"1": 0.82, ...}, "female": {"1": 0.78, ...}}}
    """
    result = np.empty(n_rows, dtype=object)
    fb_codes, fb_probs = _normalize_distribution_weights(meta.get('distribution') or {})
    z_shift = meta.get('z_shift') or {}

    def _sample_group(mask, codes, probs):
        n = int(mask.sum())
        if n == 0 or not codes:
            return
        if z_vec is not None and z_shift:
            zs = np.array([float(z_shift.get(str(c), 0.0)) for c in codes])
            z_g = z_vec[mask]
            log_w = np.log(np.maximum(np.array(probs, dtype=float), 1e-12))[:, None] + zs[:, None] * z_g[None, :]
            log_w -= log_w.max(axis=0, keepdims=True)
            w = np.exp(log_w)
            w /= w.sum(axis=0, keepdims=True)
            u = rng.random(n)
            idx = np.clip((np.cumsum(w, axis=0) < u[None, :]).sum(axis=0), 0, len(codes) - 1)
            result[mask] = np.array(codes)[idx]
        else:
            result[mask] = rng.choice(codes, size=n, p=probs)

    def _flat_dist(val):
        """Returner flat fordeling; merge kjønns-undernøkler hvis de finnes."""
        if not isinstance(val, dict):
            return {}
        first = next(iter(val.values()), None)
        if not isinstance(first, dict):
            return val
        merged = {}
        for d in val.values():
            for k, v in d.items():
                merged[k] = merged.get(k, 0.0) + float(v)
        total = sum(merged.values())
        return {k: v / total for k, v in merged.items()} if total > 0 else {}

    age_dist = meta.get('age_distribution')
    gender_dist = meta.get('gender_distribution')

    if age_dist is not None and ages_vec is not None:
        a = np.asarray(ages_vec, dtype=float)
        for bracket_str, bracket_val in age_dist.items():
            lo_b, hi_b = map(int, bracket_str.split('-'))
            age_mask = (a >= lo_b) & (a <= hi_b)
            if not age_mask.any():
                continue
            first_val = next(iter(bracket_val.values()), None) if isinstance(bracket_val, dict) else None
            if isinstance(first_val, dict) and gender_vec is not None:
                for g_key, g_int in (('male', 1), ('female', 2)):
                    if g_key not in bracket_val:
                        continue
                    g_mask = age_mask & (gender_vec == g_int)
                    codes, probs = _normalize_distribution_weights(bracket_val[g_key])
                    _sample_group(g_mask, codes, probs)
                remaining = age_mask & np.array([result[i] is None for i in range(n_rows)])
                if remaining.any():
                    codes, probs = _normalize_distribution_weights(_flat_dist(bracket_val))
                    _sample_group(remaining, codes, probs)
            else:
                codes, probs = _normalize_distribution_weights(_flat_dist(bracket_val))
                _sample_group(age_mask, codes, probs)
        unfilled = np.array([v is None for v in result])
        if unfilled.any() and fb_codes:
            _sample_group(unfilled, fb_codes, fb_probs)
        return result

    if gender_dist is not None and gender_vec is not None:
        for g_int in (1, 2):
            g_mask = gender_vec == g_int
            if g_mask.any() and str(g_int) in gender_dist:
                codes, probs = _normalize_distribution_weights(gender_dist[str(g_int)])
                _sample_group(g_mask, codes, probs)
        unfilled = np.array([v is None for v in result])
        if unfilled.any() and fb_codes:
            _sample_group(unfilled, fb_codes, fb_probs)
        return result

    if fb_codes:
        _sample_group(np.ones(n_rows, dtype=bool), fb_codes, fb_probs)
    return result


def _norway_hurdle_lognormal_kr(rng, log_mu_row, sigma, p_zero, as_int=True):
    """Med sannsynlighet p_zero: 0; ellers lognormal. p_zero kan være skalar eller vektor."""
    log_mu_row = np.asarray(log_mu_row, dtype=float).reshape(-1)
    n = len(log_mu_row)
    pz = np.asarray(p_zero, dtype=float).reshape(-1)
    if pz.size == 1:
        pz = np.full(n, float(pz[0]))
    mask = rng.random(n) > np.clip(pz, 0.02, 0.98)
    e = rng.standard_normal(n)
    raw = np.where(mask, np.exp(log_mu_row + float(sigma) * e), 0.0)
    if as_int:
        return np.round(raw).astype(np.int64)
    return raw.astype(float)


def _norway_demo_money_array(meta, short_name, n_rows, rng, unit_ids=None, ages=None):
    """Realistisk skjev fordeling for norske kroner; bruker samme latent z per unit_id som lønnsregler."""
    if unit_ids is None:
        uid_arr = np.arange(1, n_rows + 1, dtype=np.int64)
    else:
        uid_arr = np.asarray(unit_ids, dtype=np.int64).reshape(-1)
        n_rows = len(uid_arr)
    z = np.array([_norway_latent_z(int(u)) for u in uid_arr])
    kind = _norway_classify_money_demo(meta, short_name)
    as_int = meta.get("data_type") == "int"
    ages_arr = None
    if ages is not None:
        ages_arr = np.asarray(ages, dtype=float).reshape(-1)
        if len(ages_arr) != n_rows:
            ages_arr = None

    if kind == "wealth_net":
        log_mu_row = 13.85 + _NORWAY_LATENT_LOG_WEALTH_NET * z
        return _norway_lognormal_kr_rows(rng, log_mu_row, 1.38, as_int=True, min_v=0.0)
    if kind == "wealth_gross":
        log_mu_row = 14.42 + _NORWAY_LATENT_LOG_WEALTH_GROSS * z
        return _norway_lognormal_kr_rows(rng, log_mu_row, 1.28, as_int=True, min_v=0.0)
    if kind == "wage_fallback":
        if ages_arr is not None:
            gender_arr = np.array([_norway_synth_kjonn_from_uid(int(u)) for u in uid_arr])
            pz, log_mu_row = _norway_wage_age_gender_params(ages_arr, gender_arr, z)
            return _norway_hurdle_lognormal_kr(rng, log_mu_row, 0.52, pz, as_int=True)
        log_mu_row = 13.12 + _NORWAY_LATENT_LOG_WAGE * z
        return _norway_lognormal_kr_rows(rng, log_mu_row, 0.42, as_int=True, min_v=0.0)
    if kind == "transfer_hurdle":
        snu = (short_name or "").upper()
        # Sykepenger: aldersprofil når fødselsdato er importert (korrelasjon alder ↔ utbetaling)
        if "SYKEPENGER" in snu and ages_arr is not None:
            pz, log_mu_row = _norway_sykepenger_hurdle_params(ages_arr, z)
            return _norway_hurdle_lognormal_kr(rng, log_mu_row, 0.52, pz, as_int=as_int)
        # Øvrige stønader (AAP, foreldrepenger, FTRYG, …) — ofte null; nivå avhenger av tidligere lønn
        pz = np.clip(0.80 - _NORWAY_LATENT_TRANSFER_HURDLE_SHIFT * z, 0.52, 0.92)
        log_mu_row = 12.3 - 0.12 * z  # median ~220k for mottakere (AAP ~250k, sosial ~130k)
        return _norway_hurdle_lognormal_kr(rng, log_mu_row, 0.52, pz, as_int=as_int)
    if kind == "pension_hurdle":
        pz = np.clip(0.55 - 0.05 * z, 0.25, 0.85)
        log_mu_row = 12.5 + 0.18 * z  # median ~270k (alderspensjon ~270k, AFP ~350k)
        return _norway_hurdle_lognormal_kr(rng, log_mu_row, 0.48, pz, as_int=as_int)
    if kind == "transfer_child":
        pz = np.clip(0.48 - 0.03 * z, 0.25, 0.72)
        log_mu_row = 10.95 + 0.05 * z
        return _norway_hurdle_lognormal_kr(rng, log_mu_row, 0.38, pz, as_int=as_int)
    if kind == "debt":
        log_mu_row = 14.2 + 0.28 * z  # median ~1.5M (boliglån dominerer norsk gjeld)
        return _norway_lognormal_kr_rows(rng, log_mu_row, 1.05, as_int=True, min_v=0.0)
    if kind == "debt_unsecured":
        log_mu_row = 11.0 + 0.22 * z  # median ~60k (forbrukslån/kredittkort)
        return _norway_lognormal_kr_rows(rng, log_mu_row, 0.85, as_int=True, min_v=0.0)
    if kind == "capital_financial":
        log_mu_row = 12.55 + _NORWAY_LATENT_LOG_WEALTH_GROSS * 0.85 * z
        return _norway_lognormal_kr_rows(rng, log_mu_row, 0.95, as_int=True, min_v=0.0)
    if kind == "real_capital_stock":
        log_mu_row = 13.25 + _NORWAY_LATENT_LOG_WEALTH_NET * 0.9 * z
        return _norway_lognormal_kr_rows(rng, log_mu_row, 0.85, as_int=False, min_v=0.0)
    if kind == "real_wealth_component":
        log_mu_row = 12.85 + _NORWAY_LATENT_LOG_WEALTH_GROSS * 0.7 * z
        return _norway_lognormal_kr_rows(rng, log_mu_row, 0.9, as_int=as_int, min_v=0.0)
    if kind == "capital_gain_loss":
        pz = np.full(n_rows, 0.72)
        log_mu_row = 9.5 + 0.1 * z
        x = _norway_hurdle_lognormal_kr(rng, log_mu_row, 0.85, pz, as_int=as_int)
        neg = rng.random(n_rows) < 0.45
        x = np.where(neg & (x > 0), -x, x)
        return x
    if kind == "interest_flow":
        log_mu_row = 9.2 + 0.25 * z  # renteinntekter median ~10k (bankinnskudd)
        return _norway_lognormal_kr_rows(rng, log_mu_row, 0.9, as_int=as_int, min_v=0.0)
    if kind == "interest_expense":
        log_mu_row = 11.2 + 0.35 * z  # renteutgifter median ~73k (boliglånsrente)
        return _norway_lognormal_kr_rows(rng, log_mu_row, 0.85, as_int=as_int, min_v=0.0)
    if kind == "tax_amount":
        log_mu_row = 12.0 + 0.22 * z  # utlignet skatt median ~162k
        return _norway_lognormal_kr_rows(rng, log_mu_row, 0.65, as_int=True, min_v=0.0)
    if kind in ("income_total", "income_generic"):
        log_mu_row = 13.2 + _NORWAY_LATENT_LOG_INCOME_OTHER * z  # bruttoinntekt median ~540k
        return _norway_lognormal_kr_rows(
            rng, log_mu_row, 0.55, as_int=as_int, min_v=0.0
        )
    log_mu_row = 12.9 + _NORWAY_LATENT_LOG_INCOME_OTHER * z
    return _norway_lognormal_kr_rows(
        rng, log_mu_row, 0.5, as_int=as_int, min_v=0.0
    )


# Map detailed BEFOLKNING_REGSTAT_FAMTYP codes (25-code Norwegian standard,
# e.g. "2.1.1") to the legacy 5-code aggregate used by the dispatch below.
# Old codes "1"-"5" pass through unchanged.
_FAMTYP_AGGREGATE_MAP = {
    # 1.1.* — Enpersonfamilier -> legacy "1"
    "1.1.1": "1", "1.1.2": "1", "1.1.3": "1", "1.1.4": "1",
    # 2.1.* / 2.2.* — Par med barn under 18 -> legacy "3"
    "2.1.1": "3", "2.1.2": "3", "2.2.1": "3", "2.2.2": "3",
    # 2.3.* / 2.4.* — Enslig forsørger med barn under 18 -> legacy "4"
    "2.3.1": "4", "2.3.2": "4", "2.4.1": "4", "2.4.2": "4",
    # 3.1.* — Par uten barn -> legacy "2"
    "3.1.1": "2", "3.1.2": "2", "3.1.3": "2", "3.1.4": "2",
    "3.1.5": "2", "3.1.6": "2", "3.1.7": "2", "3.1.8": "2",
    # 3.2.* — Par med voksne barn -> legacy "3" (children still at home)
    "3.2.1": "3", "3.2.2": "3",
    # 3.3.* — Enslig med voksne barn -> legacy "4"
    "3.3.1": "4", "3.3.2": "4",
    # 3.4.1 — Andre familier -> legacy "5"
    "3.4.1": "5",
}


def _famtyp_to_aggregate(f) -> str:
    """Convert a FAMTYP code (detailed or legacy) to the legacy 5-code aggregate.

    Detailed codes like '2.1.1' are mapped via _FAMTYP_AGGREGATE_MAP.
    Legacy codes '1'..'5' pass through unchanged. Unknown codes pass through
    so downstream dispatch can handle them with the default branch.
    """
    s = str(f).strip()
    return _FAMTYP_AGGREGATE_MAP.get(s, s)


def _norway_demo_structure_array(short_name, n_rows, rng, current_df=None):
    """Realistiske fordelinger for BEFOLKNING_*-teller; kobles til familietype når den finnes."""
    sn = (short_name or "").upper()
    cdf = current_df
    has_fam = (
        cdf is not None
        and not getattr(cdf, "empty", True)
        and "BEFOLKNING_REGSTAT_FAMTYP" in cdf.columns
    )

    if sn == "BEFOLKNING_BARN_I_HUSH" and has_fam:
        out = np.zeros(n_rows, dtype=np.int64)
        ft = cdf["BEFOLKNING_REGSTAT_FAMTYP"].astype(str).str.strip().values
        uu = cdf[_get_df_key_col(cdf) or "unit_id"].values
        for i in range(n_rows):
            r = np.random.default_rng(_norway_demo_unit_seed(int(uu[i]), "barnhush"))
            f = _famtyp_to_aggregate(ft[i])
            if f in ("1", "2"):
                out[i] = 0
            elif f == "3":
                out[i] = int(r.choice([1, 2, 3, 4], p=[0.22, 0.38, 0.28, 0.12]))
            elif f == "4":
                out[i] = int(r.choice([1, 2, 3], p=[0.42, 0.38, 0.20]))
            elif f == "5":
                out[i] = int(r.choice([0, 1, 2], p=[0.25, 0.45, 0.30]))
            else:
                out[i] = int(r.choice([0, 1, 2], p=[0.45, 0.35, 0.20]))
        return out

    if sn == "BEFOLKNING_PERS17MIN_I_HUSHNR" and has_fam:
        out = np.zeros(n_rows, dtype=np.int64)
        ft = cdf["BEFOLKNING_REGSTAT_FAMTYP"].astype(str).str.strip().values
        uu = cdf[_get_df_key_col(cdf) or "unit_id"].values
        for i in range(n_rows):
            r = np.random.default_rng(_norway_demo_unit_seed(int(uu[i]), "pers17"))
            f = _famtyp_to_aggregate(ft[i])
            if f in ("1", "2"):
                out[i] = int(r.choice([0, 1], p=[0.94, 0.06]))
            elif f == "3":
                out[i] = int(r.choice([1, 2, 3, 4], p=[0.18, 0.36, 0.32, 0.14]))
            elif f == "4":
                out[i] = int(r.choice([1, 2, 3, 4], p=[0.32, 0.36, 0.22, 0.10]))
            else:
                out[i] = int(r.choice([0, 1, 2, 3], p=[0.45, 0.30, 0.18, 0.07]))
        return out

    if sn == "BEFOLKNING_PERS_I_HUSHNR":
        codes = np.array([1, 2, 3, 4, 5, 6], dtype=np.int64)
        p = np.array([0.16, 0.32, 0.26, 0.17, 0.06, 0.03])
        return rng.choice(codes, size=n_rows, p=p)
    if sn == "BEFOLKNING_PERS17MIN_I_HUSHNR":
        codes = np.arange(0, 8, dtype=np.int64)
        p = np.array([0.52, 0.22, 0.14, 0.07, 0.03, 0.01, 0.006, 0.004])
        return rng.choice(codes, size=n_rows, p=p)
    if sn == "BEFOLKNING_PERS18PLUS_I_HUSHNR":
        codes = np.array([1, 2, 3, 4, 5], dtype=np.int64)
        p = np.array([0.22, 0.42, 0.24, 0.09, 0.03])
        return rng.choice(codes, size=n_rows, p=p)
    if sn == "BEFOLKNING_BARN_I_HUSH":
        codes = np.arange(0, 7, dtype=np.int64)
        p = np.array([0.48, 0.24, 0.16, 0.08, 0.03, 0.007, 0.003])
        return rng.choice(codes, size=n_rows, p=p)
    return None


class MockDataEngine:
    # Mangler egen labels i variable_metadata → slå sammen med BOSATTEFDT_BOSTED / BOSATT_KOMMUNE ved import.
    _KOMMUNE_MERGE_NAMES = frozenset({
        'BEFOLKNING_KOMMNR_FORMELL',
        'BEFOLKNING_KOMMNR_FAKTISK',
        'BEFOLKNING_FOEDEKOMMNR',
        'BEFOLKNING_SVALBARD_KOMMNR',
        'BOSATT_KOMMUNE',
        'BOSATTEFDT_BOSTED',
        'KOMMNR_FORMELL',
        'KOMMNR_FAKTISK',
        'ARBLONN_ARB_ARBKOMM',
        'ARBLONN_PERS_KOMMNR',
        'ARBSTATUS_ARB_KOMM_NR',
        'ARBSTATUS_PERS_KOMM_NR',
        'BARNEVERN_KOMM',
        'INTRO_AVSL_OPPFOLG_KOMMNR',
        'INTRO_OPPFOLG_KOMMNR',
        'NUDB_KURS_SKOLEKOM',
        'REGSYS_ARB_ARBKOMM',
        'REGSYS_ARBKOMM',
        'SOSHJELP_KOMMUNE',
        'TRAFULYK_KOMMUNE',
        'VALG_MANNTALL_KOMMNR',
        'ELHUB_PERS_MALEPUNKT_ADR_KOMMUNE',
    })

    def __init__(self, default_rows=10000, metadata_path=None, catalog=None):
        self.default_rows = default_rows
        self.catalog = {}
        self._catalog_by_short = {}
        self.rule_based = {}
        self._external_meta_cache = {}
        # Globalt person-univers: deles av alle datasett slik at person-IDer
        # er konsistente uavhengig av importrekkefølge (person, jobb, NPR, …).
        self._person_universe = None
        # Nettleser/Pyodide: side-URL for å gjøre relative codelist-stier om til full URL (open_url).
        self._page_base_url = None
        # Først: last fra metadata_path slik at external_metadata blir tolket og slått sammen (CLI/skript-modus).
        if metadata_path:
            self._load_metadata(metadata_path)
        # Deretter: eventuelt overstyr/utfyll med eksplisitt katalog (nettklient/pyodide m.m.).
        if catalog is not None:
            for k, v in catalog.items():
                if k in self.catalog and isinstance(self.catalog[k], dict) and isinstance(v, dict):
                    # Behold alt vi allerede har (inkl. merged external_metadata) og la inline/eksplisitt katalog
                    # kun overstyre/legge til enkeltfelt.
                    base = self.catalog[k]
                    self.catalog[k] = {**base, **v}
                else:
                    self.catalog[k] = v
            # Pyodide/nettleser: IKKE slå sammen alle external_metadata ved oppstart — det er tungt og
            # open_url('codelists/...') feiler ofte uten riktig basis-URL. Detaljer lastes ved import
            # via ensure_variable_resolved (lazy).
            # Oppdater short-name-indeksen
            self._catalog_by_short = {k.split('/')[-1]: v for k, v in self.catalog.items()}
        # Sikkerhetsnett: fyll inn demo-fallback hvis runtime katalog mangler labels/distribution.
        for name, fallback_meta in _DEMO_FALLBACK_META.items():
            base = self.catalog.get(name)
            base_ok = isinstance(base, dict) and base
            if not base_ok:
                self.catalog[name] = dict(fallback_meta)
            else:
                # Ikke legg demo-metadata oppå variabler som har ekstern fil — de skal fylles ved import.
                if base.get("external_metadata"):
                    continue
                # Kun fyll på hvis nøkkeldata mangler.
                if "labels" not in base and "labels" in fallback_meta:
                    base["labels"] = fallback_meta["labels"]
                if "distribution" not in base and "distribution" in fallback_meta:
                    base["distribution"] = fallback_meta["distribution"]
                if "data_type" not in base and "data_type" in fallback_meta:
                    base["data_type"] = fallback_meta["data_type"]
                if "microdata_datatype" not in base and "microdata_datatype" in fallback_meta:
                    base["microdata_datatype"] = fallback_meta["microdata_datatype"]
        # Oppdater short-name-indeksen etter fallback
        self._catalog_by_short = {k.split('/')[-1]: v for k, v in self.catalog.items()}
        # Auto-sett entity_type fra enhetstype (JSON-felt) for variabler som mangler det
        for _v in self.catalog.values():
            if isinstance(_v, dict) and 'entity_type' not in _v:
                _et = _v.get('enhetstype')
                if _et and _et in _ENHETSTYPE_TO_ENTITY:
                    _v['entity_type'] = _ENHETSTYPE_TO_ENTITY[_et]
        # Propager BOSATT_KOMMUNE sin realism-blokk til de øvrige kommune-variablene
        # som deler kodeverk via _KOMMUNE_MERGE_NAMES. De har samme tidsregimer
        # (2020-reformen, 2024-splittingen) og trenger samme by_date-dekning.
        _bosatt = self.catalog.get('BOSATT_KOMMUNE') or self._catalog_by_short.get('BOSATT_KOMMUNE') or {}
        _bosatt_realism = _bosatt.get('realism') if isinstance(_bosatt, dict) else None
        if _bosatt_realism:
            for _kn in self._KOMMUNE_MERGE_NAMES:
                if _kn == 'BOSATT_KOMMUNE':
                    continue
                _target = self.catalog.get(_kn) or self._catalog_by_short.get(_kn)
                if isinstance(_target, dict) and 'realism' not in _target:
                    _target['realism'] = _bosatt_realism  # delt skrivebeskyttet referanse

    @property
    def person_universe(self) -> np.ndarray:
        """Stabilt person-ID-univers som deles av alle datasett (person, jobb, NPR, …)."""
        if self._person_universe is None:
            self._person_universe = np.arange(1, self.default_rows + 1, dtype=np.int64)
        return self._person_universe

    def _page_base_url_from_js(self):
        """Basis-URL for siden (f.eks. http://localhost:8000/microdata/) slik at codelists/foo.json kan åpnes i Pyodide."""
        if self._page_base_url:
            u = str(self._page_base_url).strip()
            if u.endswith('/'):
                return u
            return u + '/'
        try:
            import js  # type: ignore

            href = str(js.location.href)
            if href and '/' in href:
                base = href.rsplit('/', 1)[0] + '/'
                self._page_base_url = base
                return base
        except Exception:
            pass
        return None

    def _fetch_external_json(self, ext_path: str) -> dict:
        """Hent én ekstern metadata-JSON (codelists/...). Cachet per sti/URL ved suksess."""
        if not ext_path or not isinstance(ext_path, str):
            return {}
        key = ext_path.strip().replace('\\', '/')
        ck = 'ok:' + key
        if ck in self._external_meta_cache:
            return dict(self._external_meta_cache[ck])

        # 1) Lokal fil (CLI når cwd peker på microdata-mappen)
        try:
            cand = (Path.cwd() / key).resolve()
            if cand.is_file():
                with open(cand, encoding='utf-8') as ef:
                    data = json.load(ef)
                    self._external_meta_cache[ck] = data
                    return dict(data)
        except Exception:
            pass

        # 2) Pyodide: prøv relativ sti, deretter side-basis + relativ sti
        urls = []
        if key.startswith('http://') or key.startswith('https://'):
            urls.append(key)
        else:
            urls.append(key)
            base = self._page_base_url_from_js()
            if base:
                rel = key.lstrip('./')
                urls.append(base + rel)

        seen = set()
        for url in urls:
            if not url or url in seen:
                continue
            seen.add(url)
            uk = 'okurl:' + url
            if uk in self._external_meta_cache:
                data = self._external_meta_cache[uk]
                self._external_meta_cache[ck] = data
                return dict(data)
            try:
                from pyodide.http import open_url  # type: ignore

                resp = open_url(url)
                raw = resp.read()
                if isinstance(raw, bytes):
                    text = raw.decode('utf-8', errors='replace')
                else:
                    text = str(raw)
                data = json.loads(text)
                self._external_meta_cache[uk] = data
                self._external_meta_cache[ck] = data
                return dict(data)
            except Exception:
                continue
        return {}

    def ensure_variable_resolved(self, short_name: str) -> None:
        """Lazy: slå inn external_metadata for én variabel når den skal brukes (import/generering).

        Når ekstern fil finnes: **ekstern metadata overstyrer** inline/stub i variable_metadata.json
        (samme som _load_metadata). Stubs kan kun inneholde f.eks. ``external_metadata``-pekeren.
        """
        if not short_name:
            return
        meta = self.catalog.get(short_name)
        if not isinstance(meta, dict):
            return
        if meta.get('_external_merged_v1'):
            return
        ext_path = meta.get('external_metadata')
        if not ext_path:
            meta['_external_merged_v1'] = True
            self._catalog_by_short[short_name] = meta
            return

        ext_meta = self._fetch_external_json(str(ext_path))
        if ext_meta:
            # Ekstern fil er autoritativ; inline/stub (meta) fyller bare inn felt som ikke finnes i ext
            merged = {**meta, **ext_meta}
            merged['_external_merged_v1'] = True
            self.catalog[short_name] = merged
        else:
            stub = dict(meta)
            stub['_external_merged_v1'] = True
            # Nettverk/feil: bruk innebygd reservekun for kjente store variabler
            fb = _DEMO_FALLBACK_META.get(short_name)
            if fb:
                if not stub.get('labels') and 'labels' in fb:
                    stub['labels'] = dict(fb['labels'])
                if not stub.get('distribution') and 'distribution' in fb:
                    stub['distribution'] = dict(fb['distribution'])
                if not stub.get('data_type') and 'data_type' in fb:
                    stub['data_type'] = fb['data_type']
                if not stub.get('microdata_datatype') and 'microdata_datatype' in fb:
                    stub['microdata_datatype'] = fb['microdata_datatype']
            self.catalog[short_name] = stub
        self._catalog_by_short[short_name] = self.catalog[short_name]

    def _load_metadata(self, path):
        """Les variable_metadata.json med variables (katalog) og rule_based."""
        p = Path(path)
        if not p.exists():
            return
        with open(p, encoding='utf-8') as f:
            data = json.load(f)
        raw_catalog = data.get('variables', data)  # støtte for flat eller nested
        catalog = {}
        # Støtt ekstern metadata per variabel via feltet "external_metadata",
        # som peker til en JSON-fil med samme struktur som én variabel i variable_metadata.json.
        # Ekstern fil overstyrer inline/stub i hovedfilen.
        for name, meta in raw_catalog.items():
            if isinstance(meta, dict) and 'external_metadata' in meta:
                ext_path = meta['external_metadata']
                try:
                    ext_file = (p.parent / ext_path).resolve()
                    with open(ext_file, encoding='utf-8') as ef:
                        ext_meta = json.load(ef)
                    merged = {**meta, **ext_meta}
                except Exception:
                    merged = meta
                catalog[name] = merged
            else:
                catalog[name] = meta
        self.catalog = catalog
        # Keys are NAME only; _catalog_by_short allows lookup by short name (same as catalog when keys are NAME).
        self._catalog_by_short = {k.split('/')[-1]: v for k, v in self.catalog.items()}
        for rule in data.get('rule_based', []):
            v = rule['variable']
            self.rule_based[v] = rule

    def _build_rule_context_df(self, current_df, rule_def, n_rows, rng):
        """Fyll avhengigheter for rule_based: koble BEFOLKNING_* til fd/ARBEIDSSOKER_* eller syntetiser stabilt per unit_id."""
        deps = rule_def.get('dependencies', []) or []
        if current_df is None or getattr(current_df, 'empty', True):
            base = pd.DataFrame({'unit_id': np.arange(1, n_rows + 1, dtype=np.int64)})
        else:
            base = current_df.reset_index(drop=True).copy()
            n_rows = len(base)
        _uid_col = _get_df_key_col(base) or 'unit_id'
        if _uid_col not in base.columns:
            base[_uid_col] = np.arange(1, n_rows + 1, dtype=np.int64)

        def _ensure_series(name, values):
            base[name] = values if len(values) == len(base) else np.resize(values, len(base))

        for dep in deps:
            if dep in base.columns:
                continue
            if dep == 'fd/ARBEIDSSOKER_KJOENN' and 'BEFOLKNING_KJOENN' in base.columns:
                _ensure_series(dep, base['BEFOLKNING_KJOENN'].values)
            elif dep == 'fd/ARBEIDSSOKER_ALDER' and 'BEFOLKNING_FOEDSELS_AAR_MND' in base.columns:
                bym = pd.to_numeric(base['BEFOLKNING_FOEDSELS_AAR_MND'], errors='coerce').fillna(198505).astype(np.int64)
                ages = (_DEMO_REF_YEAR - (bym // 100)).clip(0, 110)
                _ensure_series(dep, ages.values)
            elif dep == 'fd/ARBEIDSSOKER_KJOENN':
                u = base[_uid_col].values
                _ensure_series(dep, [_norway_synth_kjonn_from_uid(x) for x in u])
            elif dep == 'fd/ARBEIDSSOKER_ALDER':
                u = base[_uid_col].values
                _ensure_series(dep, [_norway_synth_age_from_uid(x) for x in u])
            elif dep == 'gender':
                u = base[_uid_col].values
                _ensure_series(dep, [_norway_synth_kjonn_from_uid(x) for x in u])
            elif dep == 'age':
                u = base[_uid_col].values
                _ensure_series(dep, [_norway_synth_age_from_uid(x) for x in u])
        return base

    def _generate_from_rules(self, var_name, rule_def, current_df, rng):
        """Generer verdier basert på regler med dependencies."""
        deps = rule_def.get('dependencies', [])
        rules = rule_def.get('rules', [])
        vals = []
        for i in range(len(current_df)):
            row = current_df.iloc[i]
            chosen = None
            for r in rules:
                if r.get('fallback'):
                    chosen = r
                    continue
                cond = r.get('condition', {})
                match = True
                for k, v in cond.items():
                    if k not in row.index:
                        match = False
                        break
                    cv = row[k]
                    if not _rule_cond_value_equal(cv, v):
                        match = False
                        break
                if match:
                    chosen = r
                    break
            if chosen is None:
                chosen = next((r for r in rules if r.get('fallback')), rules[-1])

            # 1) Diskret fordeling (eksisterende mekanisme); vekter normaliseres til sannsynligheter
            if 'distribution' in chosen:
                dist = chosen.get('distribution') or {}
                if dist:
                    codes, probs = _normalize_distribution_weights(dist)
                    if codes:
                        vals.append(rng.choice(codes, p=probs))
                    continue

            # 2) Kontinuerlige fordelinger
            x = None
            if 'normal' in chosen:
                params = chosen['normal'] or {}
                mu = params.get('mean', 0.0)
                sigma = params.get('std', 1.0)
                x = float(rng.normal(mu, sigma))
            elif 'lognormal' in chosen:
                params = chosen['lognormal'] or {}
                mean = params.get('mean', 0.0)
                sigma = params.get('sigma', 1.0)
                uid = row["unit_id"] if "unit_id" in row.index else (i + 1)
                if var_name == "INNTEKT_WLONN":
                    mean = float(mean) + _NORWAY_LATENT_LOG_WAGE * _norway_latent_z(int(uid))
                x = float(rng.lognormal(mean=mean, sigma=sigma))
            elif 'uniform' in chosen:
                params = chosen['uniform'] or {}
                lo = params.get('low', 0.0)
                hi = params.get('high', 1.0)
                x = float(rng.uniform(lo, hi))
            elif 'exponential' in chosen:
                params = chosen['exponential'] or {}
                scale = params.get('scale', 1.0)
                x = float(rng.exponential(scale))

            # 3) Fallback om ingen fordeling er angitt
            if x is None:
                # Dersom ingen passende spesifikasjon: bruk 0 som nøytral fallback
                vals.append(0)
                continue

            # 4) Heltall vs. desimaler
            as_int = bool(chosen.get('as_int', False))
            # Hvis variabelen er definert som int i katalogen, favoriser int som default
            meta = self.catalog.get(var_name) or getattr(self, '_catalog_by_short', {}).get(var_name) or {}
            if meta.get('data_type') == 'int' and chosen.get('as_int') is None:
                as_int = True

            if as_int:
                lo = meta.get('min')
                hi = meta.get('max')
                if lo is not None:
                    x = max(lo, x)
                if hi is not None:
                    x = min(hi, x)
                vals.append(int(round(x)))
            else:
                vals.append(x)

        meta_ret = self.catalog.get(var_name) or getattr(self, '_catalog_by_short', {}).get(var_name) or {}
        if meta_ret.get('data_type') == 'float':
            return [float(v) for v in vals]
        return vals

    def _generate_panel(self, vars_list, dates_list):
        """Import-panel: flere variabler på flere tidspunkt. Returnerer langt format."""
        n_units = self.default_rows
        uids = np.arange(1, n_units + 1, dtype=np.int64)
        tid_vals = [int(d[:4]) if len(d) >= 4 else int(d) for d in dates_list] or [2010, 2011, 2012]
        rows = []
        # Bygg date@panel fra tid-verdiene (YYYY -> YYYY-01-01)
        date_map = {t: pd.Timestamp(f"{t}-01-01") for t in tid_vals}

        for uid in uids:
            for tid in tid_vals:
                row = {'unit_id': uid, 'tid': tid, 'date@panel': date_map[tid]}
                for var_path in vars_list:
                    vname = var_path.split('/')[-1]
                    self.ensure_variable_resolved(vname)
                    seed = int(hashlib.md5(f"{vname}_{uid}_{tid}".encode()).hexdigest(), 16) % (10**8)
                    rng = np.random.default_rng(seed)
                    meta = self.catalog.get(vname) or getattr(self, '_catalog_by_short', {}).get(vname) or {}
                    if meta.get('distribution'):
                        codes, probs = _normalize_distribution_weights(meta['distribution'])
                        if codes:
                            row[vname] = int(rng.choice(codes, p=probs)) if str(codes[0]).isdigit() else rng.choice(codes, p=probs)
                    elif meta.get('labels') and isinstance(meta.get('labels'), dict):
                        codes = list(meta['labels'].keys())
                        key_to_int = lambda k: int(k) if isinstance(k, str) and (k.lstrip('-').isdigit()) else k
                        code_ints = [key_to_int(k) for k in codes]
                        row[vname] = int(rng.choice(code_ints))
                    elif meta.get('min') is not None or meta.get('max') is not None:
                        if _norway_classify_money_demo(meta, vname):
                            arr = _norway_demo_money_array(meta, vname, 1, rng, unit_ids=np.array([uid]))
                            v = int(arr[0]) if str(meta.get('data_type', '')).lower() == 'int' else float(arr[0])
                            lo = meta.get('min')
                            if lo is not None:
                                v = max(v, int(lo))
                            row[vname] = v
                        else:
                            lo = meta.get('min', 0)
                            hi = meta.get('max', 9999)
                            row[vname] = int(rng.integers(lo, hi + 1))
                    elif meta.get('mean') is not None or meta.get('std') is not None:
                        m, s = meta.get('mean', 500000), meta.get('std', 100000)
                        dt = str(meta.get('data_type', '')).lower()
                        if _norway_classify_money_demo(meta, vname):
                            arr = _norway_demo_money_array(meta, vname, 1, rng, unit_ids=np.array([uid]))
                            v = int(arr[0]) if dt == 'int' else float(arr[0])
                            lo, hi = meta.get('min'), meta.get('max')
                            if dt == 'int' and lo is not None:
                                v = max(v, int(lo))
                            if dt == 'int' and hi is not None:
                                v = min(v, int(hi))
                            row[vname] = v
                        elif dt == 'int':
                            v = int(round(rng.normal(m, s)))
                            lo, hi = meta.get('min'), meta.get('max')
                            if lo is not None:
                                v = max(v, int(lo))
                            if hi is not None:
                                v = min(v, int(hi))
                            row[vname] = v
                        else:
                            row[vname] = rng.normal(m, s)
                    else:
                        if _norway_classify_money_demo(meta, vname):
                            row[vname] = int(_norway_demo_money_array(meta, vname, 1, rng, unit_ids=np.array([uid]))[0])
                        else:
                            row[vname] = rng.normal(500000, 100000)
                rows.append(row)
        return pd.DataFrame(rows)

    # ── NPR-generering ───────────────────────────────────────────────────────

    def _pick_icd10(self, age, sex, rng):
        """Velg ICD-10-kode vektet etter alder og kjønn. sex: 1=mann, 2=kvinne."""
        weights = []
        for code, _label, mn, mx, gbias, bw in _ICD10_CODES:
            age_factor = 1.0 if mn <= age <= mx else 0.05
            gender_factor = max(0.001, 1.0 + gbias * (1 if sex == 2 else -1))
            if code == 'O80' and sex != 2:
                gender_factor = 0.0
            weights.append(bw * age_factor * gender_factor)
        w = np.array(weights, dtype=float)
        w /= w.sum()
        idx = rng.choice(len(_ICD10_CODES), p=w)
        return _ICD10_CODES[idx][0]

    def _generate_npr_variable(self, var_name, current_df):
        """Generer NPR-variabel. Returnerer DataFrame med unit_id + (AGGRSHOPPID +) variabel."""
        seed = int(hashlib.md5(var_name.encode()).hexdigest(), 16) % (2**31)
        rng = np.random.default_rng(seed)

        fresh = current_df is None or current_df.empty
        has_ep = not fresh and 'AGGRSHOPPID' in current_df.columns

        if fresh:
            # Første NPR-import: bygg episode-rader.
            # Sample person-IDer fra globalt person-univers slik at NPRID matcher
            # PERSONID_1 i person-datasett (uavhengig av importrekkefølge).
            n_ep = max(200, min(2000, self.default_rows // 4))
            a = rng.choice(self.person_universe, size=n_ep, replace=True)
            b = rng.choice(self.person_universe, size=n_ep, replace=True)
            unit_ids = np.sort(np.minimum(a, b))          # int64, noen person-IDer gjentas
            n = int(len(unit_ids))
            ep_ids = np.arange(1, n + 1, dtype=np.int64)
            base_df = None  # Bygges ikke lenger her — DataFrame lages komplett ved retur
        else:
            unit_ids = current_df['unit_id'].values.astype(np.int64)
            ep_ids = (current_df['AGGRSHOPPID'].values.astype(np.int64) if has_ep
                      else np.arange(1, len(current_df) + 1, dtype=np.int64))
            n = int(len(current_df))
            base_df = None

        # Deterministisk alder og kjønn per person (brukes til ICD-10-vekting)
        ages   = np.array([_norway_synth_age_from_uid(int(uid)) for uid in unit_ids], dtype=np.int64)
        gender = np.where(np.array([_norway_latent_z(int(uid)) for uid in unit_ids]) > 0, 2, 1).astype(np.int64)

        # Generer kolonneverdien
        col_name = var_name
        if var_name == 'AGGRSHOPPID':
            col_vals = ep_ids  # Allerede i base_df; bare inkluder
        elif var_name == 'NPRID':
            col_vals = unit_ids
            col_name = 'NPRID'
        elif var_name in ('HOVEDTILSTAND1', 'HOVEDTILSTAND2'):
            col_vals = np.array([self._pick_icd10(ages[i], gender[i], rng) for i in range(n)])
            if var_name == 'HOVEDTILSTAND2':
                mask = rng.random(n) < 0.60
                col_vals = col_vals.astype(object)
                col_vals[mask] = np.nan
        elif var_name == 'INNDATO':
            # Dager siden 1970-01-01, år 2015–2024
            col_vals = rng.integers(16436, 20090, size=n, dtype=np.int64)
        elif var_name == 'UTDATO':
            if not fresh and 'INNDATO' in current_df.columns:
                inn = current_df['INNDATO'].values.astype(np.float64)
            else:
                inn = rng.integers(16436, 20090, size=n, dtype=np.int64).astype(np.float64)
            omsorg = current_df['OMSORGSNIVA'].values if (not fresh and 'OMSORGSNIVA' in current_df.columns) else None
            extra = np.zeros(n, dtype=np.float64)
            for i in range(n):
                om = omsorg[i] if omsorg is not None else rng.choice(['døgn', 'dag', 'poliklinisk'], p=[0.60, 0.25, 0.15])
                if om == 'poliklinisk':
                    extra[i] = 0
                elif om == 'dag':
                    extra[i] = int(rng.integers(0, 2, dtype=np.int64))
                else:
                    extra[i] = int(rng.integers(1, 31, dtype=np.int64))
            col_vals = (inn + extra).astype(np.int64)
        elif var_name == 'INNTID':
            hours   = rng.integers(7, 23, size=n, dtype=np.int64)
            minutes = rng.integers(0, 4, size=n, dtype=np.int64) * 15
            col_vals = np.array([f"{int(h):02d}{int(m):02d}" for h, m in zip(hours, minutes)])
        elif var_name == 'UTTID':
            hours   = rng.integers(8, 19, size=n, dtype=np.int64)
            minutes = rng.integers(0, 4, size=n, dtype=np.int64) * 15
            col_vals = np.array([f"{int(h):02d}{int(m):02d}" for h, m in zip(hours, minutes)])
        elif var_name == 'OMSORGSNIVA':
            keys = list(_NPR_OMSORG_DIST.keys())
            probs = list(_NPR_OMSORG_DIST.values())
            col_vals = rng.choice(keys, size=n, p=probs)
        elif var_name == 'NIVA':
            keys = list(_NPR_NIVA_DIST.keys())
            probs = list(_NPR_NIVA_DIST.values())
            col_vals = rng.choice(keys, size=n, p=probs)
        else:
            col_vals = np.zeros(n, dtype=np.int64)

        # Bygg DataFrame med ALLE kolonner på én gang for å unngå block-konsolideringsfeil
        # i Pyodide/pandas 1.x (int64 → int32 cast ved inkrementell kolonnelegging).
        if fresh:
            if col_name == 'AGGRSHOPPID':
                return pd.DataFrame({'unit_id': unit_ids, 'AGGRSHOPPID': ep_ids})
            else:
                return pd.DataFrame({'unit_id': unit_ids, 'AGGRSHOPPID': ep_ids, col_name: col_vals})
        else:
            if col_name == 'AGGRSHOPPID':
                return pd.DataFrame({'AGGRSHOPPID': ep_ids})
            else:
                return pd.DataFrame({'AGGRSHOPPID': ep_ids, col_name: col_vals})

    def _generate_multi_record_entity(self, entity_type, short_name, var_name,
                                      parsed_args, meta):
        """Generer multi-record data for ikke-person-enheter (jobb, kjøretøy, kurs).

        Første import i et tomt datasett: bygger 1:N-struktur der hver person
        har 0..max enheter basert på _ENTITY_MULTI_RECORD_PROFILE.
        Returnerer DataFrame med [entity_id_col, person_ref_col, var_name].
        """
        profile = _ENTITY_MULTI_RECORD_PROFILE[entity_type]
        p_has = profile['p_has']
        mean_count = profile['mean']
        max_count = profile['max']

        seed = int(hashlib.md5(f"multirecord_{entity_type}".encode()).hexdigest(), 16) % (2**31)
        rng = np.random.default_rng(seed)

        persons = self.person_universe
        n_persons = len(persons)

        # Hvem har minst én enhet?
        has_record = rng.random(n_persons) < p_has
        persons_with = persons[has_record]
        n_with = int(has_record.sum())

        if n_with == 0:
            id_col = _ENTITY_ID_COL.get(entity_type, 'unit_id')
            ref_col = _ENTITY_PERSON_REF_COL.get(entity_type, 'person_ref')
            return pd.DataFrame({id_col: [], ref_col: [], var_name: []})

        # Antall enheter per person (Poisson, minst 1, maks max_count)
        counts = np.clip(rng.poisson(max(0.1, mean_count), n_with), 1, max_count)
        total = int(counts.sum())

        # Person-IDer gjentatt per enhet
        person_ids = np.repeat(persons_with, counts.astype(int)).tolist()
        person_ids = np.array(person_ids, dtype=np.int64)

        # Enhets-IDer (løpenummer)
        entity_ids = np.arange(1, total + 1, dtype=np.int64)

        id_col = _ENTITY_ID_COL.get(entity_type, 'unit_id')
        ref_col = _ENTITY_PERSON_REF_COL.get(entity_type, 'person_ref')

        # Generer variabelverdier med person-basert latent-z for realisme
        var_seed = int(hashlib.md5(var_name.encode()).hexdigest(), 16) % (10**8)
        var_rng = np.random.default_rng(var_seed)

        ages_vec = np.array([_norway_synth_age_from_uid(int(u)) for u in person_ids], dtype=float)
        gender_vec = np.array([_norway_synth_kjonn_from_uid(int(u)) for u in person_ids], dtype=np.int8)
        z_vec = np.array([_norway_latent_z(int(u)) for u in person_ids], dtype=float)

        self.ensure_variable_resolved(short_name)
        var_meta = (self.catalog.get(short_name)
                    or self.catalog.get(var_name)
                    or getattr(self, '_catalog_by_short', {}).get(short_name)
                    or meta or {})

        # Generer verdier basert på metadata (distribution/labels/mean+std)
        vals = self._generate_variable_values(
            var_name, short_name, var_meta, total, var_rng,
            uids=person_ids, ages_vec=ages_vec, gender_vec=gender_vec, z_vec=z_vec,
            parsed_args=parsed_args)

        return pd.DataFrame({
            id_col: entity_ids,
            ref_col: person_ids,
            var_name: vals,
        })

    def _generate_variable_values(self, var_name, short_name, meta, n_rows, rng,
                                  uids=None, ages_vec=None, gender_vec=None, z_vec=None,
                                  parsed_args=None):
        """Generer verdier for én variabel basert på metadata.

        Brukes av _generate_multi_record_entity() for første import i multi-record-datasett.
        Dekker samme kodestier som generate() for å sikre at realisme-mekanismene er aktive.
        """
        micro_dt = str(meta.get('microdata_datatype', '')).lower()
        data_type = str(meta.get('data_type', '')).lower()
        is_alfanumerisk = 'alfanumerisk' in micro_dt or meta.get('data_type') == 'string'

        # NUS-kodegenerator
        if short_name in _NUS_GENERATOR_VARS:
            return _generate_nus_codes_vec(n_rows, rng, ages=ages_vec).tolist()

        # Realism-framework
        _realism_spec = meta.get('realism')
        if _realism_spec:
            try:
                import mockdata_realism as _mr
            except ImportError:
                _realism_spec = None
        if _realism_spec:
            _as_of = (parsed_args or {}).get('date1') or _DEMO_REF_YEAR
            _ctx_df = pd.DataFrame({'unit_id': uids}) if uids is not None else pd.DataFrame({'unit_id': np.arange(1, n_rows+1)})
            _family = str(_realism_spec.get('family', '')).lower()
            if _family == 'categorical':
                return [str(v) for v in _mr.generate_categorical(_realism_spec, _ctx_df, as_of=_as_of, rng=rng)]
            else:
                return _mr.generate_numeric(_realism_spec, _ctx_df, as_of=_as_of, rng=rng).tolist()

        # Datovariabler
        if data_type.startswith('date:yyyymmdd'):
            years = rng.integers(1990, _DEMO_REF_YEAR + 1, size=n_rows)
            months = rng.integers(1, 13, size=n_rows)
            days = rng.integers(1, 29, size=n_rows)
            return (years * 10000 + months * 100 + days).astype(int).tolist()
        if data_type.startswith('date:yyyymm'):
            _d1 = (parsed_args or {}).get('date1')
            ref_year = int(str(_d1)[:4]) if _d1 else _DEMO_REF_YEAR
            ages = rng.normal(loc=44, scale=21, size=n_rows)
            ages = np.clip(ages, 0, 100).astype(int)
            years = np.clip(ref_year - ages, 1900, ref_year)
            months = rng.integers(1, 13, size=n_rows)
            return (years * 100 + months).astype(int).tolist()
        if data_type.startswith('date:epoch'):
            years = rng.integers(1990, 2026, size=n_rows)
            months = rng.integers(1, 13, size=n_rows)
            days = rng.integers(1, 29, size=n_rows)
            dates = pd.to_datetime(dict(year=years, month=months, day=days))
            return (dates - pd.Timestamp('1970-01-01')).dt.days.astype(int).tolist()

        # Konstantvariabler
        if meta.get('type') == 'constant':
            return [meta.get('value', 0)] * n_rows

        # Kondisjonert kategorisk (alder/kjønn/latent-z)
        if meta.get('age_distribution') or meta.get('gender_distribution') or (meta.get('distribution') and meta.get('z_shift')):
            raw = _norway_sample_categorical(meta, n_rows, rng,
                                             ages_vec=ages_vec, gender_vec=gender_vec, z_vec=z_vec)
            if is_alfanumerisk:
                return [str(c) if c is not None else '0' for c in raw]
            return [int(c) if isinstance(c, str) and str(c).isdigit() else (int(c) if c is not None else 0) for c in raw]

        # Kategorisk med flat distribusjon
        if meta.get('distribution'):
            codes, probs = _normalize_distribution_weights(meta['distribution'])
            if codes:
                raw = rng.choice(codes, size=n_rows, p=probs)
            else:
                raw = rng.choice(list(meta.get('labels', {}).keys()) or [0], size=n_rows)
            if is_alfanumerisk:
                return [str(c) for c in raw]
            return [int(c) if isinstance(c, str) and c.isdigit() else c for c in raw]

        # Labels uten distribusjon (uniformt)
        labels = meta.get('labels', meta.get('labels_dict'))
        if isinstance(labels, dict) and labels:
            codes = list(labels.keys())
            raw = rng.choice(codes, size=n_rows)
            if is_alfanumerisk:
                return [str(x) for x in raw]
            return [int(x) if isinstance(x, str) and x.lstrip('-').isdigit() else x for x in raw]

        # Pengebeløp (lognormal med latent-z)
        if _norway_classify_money_demo(meta, short_name):
            return _norway_demo_money_array(
                meta, short_name, n_rows, rng, unit_ids=uids, ages=ages_vec
            ).tolist()

        # Numerisk (mean/std)
        m = meta.get('mean')
        s = meta.get('std')
        if m is not None or s is not None:
            m = m or 500000; s = s or 100000
            raw = rng.normal(m, s, n_rows)
            lo, hi = meta.get('min'), meta.get('max')
            if lo is not None: raw = np.maximum(raw, float(lo))
            if hi is not None: raw = np.minimum(raw, float(hi))
            if data_type == 'int':
                return [int(x) for x in np.round(raw)]
            return raw.tolist()

        # Min/max uten mean/std
        lo = meta.get('min')
        hi = meta.get('max')
        if lo is not None or hi is not None:
            if _norway_classify_money_demo(meta, short_name):
                arr = _norway_demo_money_array(meta, short_name, n_rows, rng, unit_ids=uids, ages=ages_vec)
                if lo is not None: arr = np.maximum(arr, int(lo))
                return arr.tolist()
            lo = lo or 0; hi = hi or 9999
            return rng.integers(int(lo), int(hi) + 1, size=n_rows).tolist()

        # Fallback
        return rng.normal(500000, 100000, n_rows).tolist()

    def generate(self, cmd, parsed_args, current_df):
        # import-panel med flere variabler: {"vars": [...], "dates": [...]}
        if cmd == 'import-panel' and 'vars' in parsed_args and 'dates' in parsed_args:
            return self._generate_panel(parsed_args['vars'], parsed_args['dates'])

        var_path = parsed_args.get('var', '')
        var_name = parsed_args.get('alias') or var_path.split('/')[-1]

        # NPR-variabel: eget genereringsløp
        short_name = var_path.split('/')[-1] if var_path else ''
        _meta_check = self.catalog.get(short_name) or self.catalog.get(var_name) or getattr(self, '_catalog_by_short', {}).get(short_name) or {}
        if _meta_check.get('entity_type') == _NPR_ENTITY:
            _npr_result = self._generate_npr_variable(short_name or var_name, current_df)
            # Rename variable column to alias if one was given (e.g. import ndb/NPRID as pid)
            if var_name != short_name and short_name in _npr_result.columns:
                _npr_result = _npr_result.rename(columns={short_name: var_name})
            return _npr_result

        # Multi-record enhetstyper (jobb, kjøretøy, kurs): bygg 1:N-struktur ved første import
        _var_entity = _meta_check.get('entity_type', 'person')
        if (cmd == 'import'
            and current_df.empty
            and _var_entity in _ENTITY_MULTI_RECORD_PROFILE):
            return self._generate_multi_record_entity(
                _var_entity, short_name, var_name, parsed_args, _meta_check)

        n_rows = self.default_rows if current_df.empty else len(current_df)
        _src_id = _get_df_key_col(current_df) or 'unit_id'
        uids = (self.person_universe[:n_rows] if current_df.empty
                else current_df[_src_id].values.astype(np.int64))

        # For ikke-person-enheter: bruk person-referansekolonnen for latent-z, alder og kjønn
        # slik at verdier korrelerer med personens egenskaper, ikke enhetens ID.
        _person_ref_col = _ENTITY_PERSON_REF_COL.get(_var_entity)
        if (_person_ref_col
            and not current_df.empty
            and _person_ref_col in current_df.columns):
            _pids = current_df[_person_ref_col].values.astype(np.int64)
        else:
            _pids = uids

        ages_vec = _norway_demo_ages_from_current_df(current_df)
        if ages_vec is None and len(_pids) > 0:
            ages_vec = np.array([_norway_synth_age_from_uid(int(u)) for u in _pids], dtype=float)
        gender_vec = (np.array([_norway_synth_kjonn_from_uid(int(u)) for u in _pids], dtype=np.int8)
                      if len(_pids) > 0 else None)
        z_vec = (np.array([_norway_latent_z(int(u)) for u in _pids], dtype=float)
                 if len(_pids) > 0 else None)

        seed = int(hashlib.md5(var_name.encode()).hexdigest(), 16) % (10**8)
        rng = np.random.default_rng(seed)

        data = {_src_id: uids}

        # Realism framework (opt-in via `realism` block in catalog metadata).
        # Takes precedence over rule_based so authors can migrate variables
        # one at a time. See mockdata_realism.py for the full spec.
        short_name = var_path.split('/')[-1] if var_path else ''
        if short_name:
            self.ensure_variable_resolved(short_name)
        _realism_meta = (
            self.catalog.get(short_name)
            or self.catalog.get(var_name)
            or getattr(self, '_catalog_by_short', {}).get(short_name)
            or {}
        )
        _realism_spec = _realism_meta.get('realism') if _realism_meta else None
        if _realism_spec:
            try:
                import mockdata_realism as _mr
            except ImportError:
                _realism_spec = None
        if _realism_spec:
            _as_of = parsed_args.get('date1') or _DEMO_REF_YEAR
            if current_df.empty:
                _ctx_df = pd.DataFrame({'unit_id': _pids})
            else:
                _ctx_df = current_df.copy()
                # For ikke-person-enheter: sett unit_id til person-IDer slik at
                # realism-rammeverket bruker person-basert latent-z.
                if _person_ref_col and _person_ref_col in _ctx_df.columns:
                    _ctx_df['unit_id'] = _ctx_df[_person_ref_col].values
                elif 'unit_id' not in _ctx_df.columns:
                    _ctx_df['unit_id'] = uids
            _family = str(_realism_spec.get('family', '')).lower()
            if _family == 'categorical':
                _vals = _mr.generate_categorical(_realism_spec, _ctx_df, as_of=_as_of, rng=rng)
                _dt = (_realism_meta.get('data_type') or '').lower()
                if _dt in ('float', 'int', 'integer', 'numeric'):
                    data[var_name] = [float(v) for v in _vals]
                else:
                    data[var_name] = [str(v) for v in _vals]
            else:
                _vals = _mr.generate_numeric(_realism_spec, _ctx_df, as_of=_as_of, rng=rng)
                data[var_name] = _vals.tolist()
            return pd.DataFrame(data)

        # Regelbasert variabel (krever at dependencies finnes i current_df). rule_based keys are NAME.
        rule_def = self.rule_based.get(var_name) or self.rule_based.get(short_name)
        if rule_def:
            n_eff = self.default_rows if current_df.empty else len(current_df)
            ctx_df = self._build_rule_context_df(current_df, rule_def, n_eff, rng)
            data[var_name] = self._generate_from_rules(var_name, rule_def, ctx_df, rng)
            return pd.DataFrame(data)

        # Katalogmetadata: lookup by variable NAME only (var_path is e.g. db/NAME from import).
        meta = self.catalog.get(short_name) or self.catalog.get(var_name) or getattr(self, '_catalog_by_short', {}).get(short_name) or {}
        # Enkel spesialhåndtering for kommunevariabler som mangler egne labels:
        # bruk kodeliste fra BOSATTEFDT_BOSTED/BOSATT_KOMMUNE som fallback slik at
        # vi genererer faktiske kommunekoder (0301, ...) og ikke kontinuerlige tall.
        # Variabler uten egen kodeliste: fyll fra BOSATT_KOMMUNE (mest komplett) / BOSATTEFDT_BOSTED.
        if not meta.get('labels') and short_name in self._KOMMUNE_MERGE_NAMES:
            self.ensure_variable_resolved('BOSATT_KOMMUNE')
            self.ensure_variable_resolved('BOSATTEFDT_BOSTED')
            base = (
                self.catalog.get('BOSATT_KOMMUNE')
                or self.catalog.get('BOSATTEFDT_BOSTED')
                or getattr(self, '_catalog_by_short', {}).get('BOSATT_KOMMUNE')
                or getattr(self, '_catalog_by_short', {}).get('BOSATTEFDT_BOSTED')
                or {}
            )
            if not (isinstance(base, dict) and isinstance(base.get('labels'), dict) and base.get('labels')):
                base = dict(_MINIMAL_KOMMUNE_BASE)
            if isinstance(base, dict) and isinstance(base.get('labels'), dict):
                # Base gir labels/distribution; meta (f.eks. FORMELL) gir type, beskrivelse osv.
                # Ikke la tom/None labels i meta overskrive base (JSON kan ha "labels": null).
                merged = dict(base)
                for k, v in meta.items():
                    if k == 'labels' and not v:
                        continue
                    if k == 'distribution' and not v:
                        continue
                    merged[k] = v
                meta = merged
                # Viktig: skriv tilbake til katalogen slik at LabelManager/tabulate ser samme metadata
                # som generatoren (ellers kan FORMELL stå uten labels i catalog).
                self.catalog[short_name] = merged
                self._catalog_by_short[short_name] = merged
        micro_dt = str(meta.get('microdata_datatype', '')).lower()
        data_type = str(meta.get('data_type', '')).lower()
        is_alfanumerisk = 'alfanumerisk' in micro_dt or meta.get('data_type') == 'string'

        if cmd == 'import':
            # Hierarkisk NUS-kodegenerator: realistiske 6-sifrede NUS2000-koder
            if short_name in _NUS_GENERATOR_VARS:
                _nus_ages = ages_vec
                if _nus_ages is None:
                    _nus_ages = np.array([_norway_synth_age_from_uid(int(u)) for u in _pids], dtype=float)
                data[var_name] = _generate_nus_codes_vec(n_rows, rng, ages=_nus_ages).tolist()
                return pd.DataFrame(data)

            # FNR-referansevariabler: sample fra globalt person-univers
            # slik at merge på disse gir reelle treff uavhengig av importrekkefølge.
            if short_name in _PERSONID_REF_VARS and len(uids) > 0:
                sampled = rng.choice(self.person_universe, size=n_rows, replace=True)
                data[var_name] = sampled.tolist()
                return pd.DataFrame(data)

            cdf_struct = None if current_df.empty else current_df
            struct = _norway_demo_structure_array(short_name, n_rows, rng, current_df=cdf_struct)
            if struct is not None:
                data[var_name] = struct.tolist()
                return pd.DataFrame(data)
            # Datovariabler: generer basert på data_type-format
            if data_type.startswith('date:yyyymmdd'):
                # YYYYMMDD-format (f.eks. dødsdato, oppdateringsdato)
                start_year = 1990
                end_year = _DEMO_REF_YEAR
                years = rng.integers(start_year, end_year + 1, size=n_rows)
                months = rng.integers(1, 13, size=n_rows)
                days = rng.integers(1, 29, size=n_rows)
                yyyymmdd = years * 10000 + months * 100 + days
                data[var_name] = yyyymmdd.astype(int)
            elif data_type.startswith('date:yyyymm'):
                # F.eks. fødselsår og -måned (BEFOLKNING_FOEDSELS_AAR_MND).
                # Bruk referansedato fra import-kallet som øvre grense —
                # ellers kan personer få fødselsdato etter en status/filter-dato.
                _d1 = parsed_args.get('date1')
                if _d1:
                    ref_year = int(str(_d1)[:4])
                    ref_month = int(str(_d1)[5:7])
                else:
                    ref_year = _DEMO_REF_YEAR
                    ref_month = 12
                ages = rng.normal(loc=44, scale=21, size=n_rows)
                ages = np.clip(ages, 0, 100).astype(int)
                years = ref_year - ages
                years = np.clip(years, 1900, ref_year)
                months = rng.integers(1, 13, size=n_rows)
                # Kapp måned for personer født i referanseåret
                at_ref_year = (years == ref_year)
                months = np.where(at_ref_year, np.minimum(months, ref_month), months)
                yyyymm = years * 100 + months
                data[var_name] = yyyymm.astype(int)
            elif data_type.startswith('date:epoch'):
                # Enkle epoch-datoer: trekk kalenderdato og konverter til dager siden 1970-01-01
                start_year = 1990
                end_year = 2025
                years = rng.integers(start_year, end_year + 1, size=n_rows)
                months = rng.integers(1, 13, size=n_rows)
                days = rng.integers(1, 29, size=n_rows)  # unngå månedslengde-problemer
                dates = pd.to_datetime(dict(year=years, month=months, day=days))
                epoch_days = (dates - pd.Timestamp('1970-01-01')).dt.days.astype(int)
                data[var_name] = epoch_days
            elif meta.get('type') == 'constant':
                if data_type.startswith('date'):
                    data[var_name] = [meta.get('value', '2000-01-01')] * n_rows
                else:
                    data[var_name] = [meta.get('value', 0)] * n_rows
            elif meta.get('age_distribution') or meta.get('gender_distribution') or (meta.get('distribution') and meta.get('z_shift')):
                # Kondisjonert kategorisk fordeling (alder, kjønn, latent-z)
                raw = _norway_sample_categorical(meta, n_rows, rng,
                                                 ages_vec=ages_vec, gender_vec=gender_vec, z_vec=z_vec)
                if is_alfanumerisk:
                    data[var_name] = [str(c) if c is not None else '0' for c in raw]
                else:
                    data[var_name] = [int(c) if isinstance(c, str) and str(c).isdigit() else (int(c) if c is not None else 0) for c in raw]
            elif meta.get('distribution'):
                # Flat kategorisk fordeling (ingen kondisjonering)
                codes, probs = _normalize_distribution_weights(meta['distribution'])
                raw = rng.choice(codes, size=n_rows, p=probs) if codes else rng.choice(list(meta.get('labels', {}).keys()) or [0], size=n_rows)
                if is_alfanumerisk:
                    data[var_name] = [str(c) for c in raw]
                elif data_type == 'float':
                    data[var_name] = [float(c) for c in raw]
                else:
                    data[var_name] = [int(c) if isinstance(c, str) and c.isdigit() else c for c in raw]
            else:
                labels = meta.get('labels', meta.get('labels_dict'))

                def _label_key_int_like(k):
                    if isinstance(k, (int, np.integer)):
                        return True
                    s = str(k)
                    if '/' in s:
                        return False
                    if '.' in s:
                        return False
                    return s.lstrip('-').isdigit()

                # Eksplisitt kontinuerlig float (mean/std) — ikke overstyr med labels
                if meta.get('data_type') == 'float' and (meta.get('mean') is not None or meta.get('std') is not None):
                    m, s = meta.get('mean', 500000), meta.get('std', 100000)
                    if _norway_classify_money_demo(meta, short_name):
                        data[var_name] = _norway_demo_money_array(
                            meta, short_name, n_rows, rng, unit_ids=uids, ages=ages_vec
                        ).astype(float)
                    else:
                        col = rng.normal(m, s, n_rows)
                        lo_f = meta.get('min')
                        if lo_f is not None:
                            col = np.maximum(col, float(lo_f))
                        hi_f = meta.get('max')
                        if hi_f is not None:
                            col = np.minimum(col, float(hi_f))
                        data[var_name] = col
                # Heltall i kroner (microdata): mean/std → avrundet normalfordeling, valgfri min/max
                elif str(meta.get('data_type', '')).lower() == 'int' and (meta.get('mean') is not None or meta.get('std') is not None):
                    m, s = meta.get('mean', 500000), meta.get('std', 100000)
                    if _norway_classify_money_demo(meta, short_name):
                        raw = _norway_demo_money_array(
                            meta, short_name, n_rows, rng, unit_ids=uids, ages=ages_vec
                        )
                    else:
                        raw = np.round(rng.normal(m, s, n_rows)).astype(np.int64)
                    lo, hi = meta.get('min'), meta.get('max')
                    if lo is not None:
                        raw = np.maximum(raw, int(lo))
                    if hi is not None:
                        raw = np.minimum(raw, int(hi))
                    data[var_name] = [int(x) for x in raw]
                elif isinstance(labels, dict) and labels:
                    # Med labels: trekk kun blant kodeverdier (uniformt hvis ingen distribution over)
                    codes_all = list(labels.keys())
                    if is_alfanumerisk or meta.get('data_type') == 'string' or not all(_label_key_int_like(k) for k in codes_all):
                        raw = rng.choice(codes_all, size=n_rows)
                        data[var_name] = [str(x) for x in raw]
                    else:
                        codes = []
                        for k in codes_all:
                            if isinstance(k, str) and k.lstrip('-').isdigit():
                                codes.append(int(k))
                            elif isinstance(k, (int, float, np.integer)):
                                codes.append(int(k))
                        if codes:
                            raw = rng.choice(codes, size=n_rows)
                            data[var_name] = [int(x) for x in raw]
                        else:
                            raw = rng.choice(codes_all, size=n_rows)
                            data[var_name] = [str(x) for x in raw]
                elif meta.get('min') is not None or meta.get('max') is not None:
                    if _norway_classify_money_demo(meta, short_name):
                        # min/max i metadata er teknisk tak for inntekts-/formuevariabler —
                        # bruk realistisk fordeling og ignorer maks-grensen
                        arr = _norway_demo_money_array(
                            meta, short_name, n_rows, rng, unit_ids=uids, ages=ages_vec
                        )
                        lo = meta.get('min')
                        if lo is not None:
                            arr = np.maximum(arr, int(lo))
                        data[var_name] = [int(x) for x in arr] if meta.get('data_type') == 'int' else arr.astype(float)
                    else:
                        lo = meta.get('min', 0)
                        hi = meta.get('max', 9999)
                        data[var_name] = [int(x) for x in rng.integers(lo, hi + 1, size=n_rows).tolist()]
                elif short_name in self._KOMMUNE_MERGE_NAMES:
                    # Trekk alltid blant kjente kommunekoder — aldri uniform -2..9999 (koder uten label i tabulate).
                    labels_k = meta.get('labels', meta.get('labels_dict'))
                    if isinstance(labels_k, dict) and labels_k:
                        codes_all = list(labels_k.keys())
                        raw = rng.choice(codes_all, size=n_rows)
                        data[var_name] = [str(x) for x in raw]
                    else:
                        fb = dict(_MINIMAL_KOMMUNE_BASE)
                        codes, probs = _normalize_distribution_weights(fb['distribution'])
                        raw = rng.choice(codes, size=n_rows, p=probs) if codes else ['0301'] * n_rows
                        data[var_name] = [str(c) for c in raw]
                else:
                    if _norway_classify_money_demo(meta, short_name):
                        arr = _norway_demo_money_array(
                            meta, short_name, n_rows, rng, unit_ids=uids, ages=ages_vec
                        )
                        data[var_name] = np.asarray(arr, dtype=float) if meta.get('data_type') == 'float' else arr
                    else:
                        data[var_name] = rng.normal(500000, 100000, n_rows)
            
        elif cmd == 'import-event':
            date1 = parsed_args.get('date1', '2010-01-01')
            date2 = parsed_args.get('date2', '2012-01-01')
            return _generate_event_rows(short_name, var_name, uids, _src_id,
                                        date1, date2, meta, rng)

        elif cmd == 'import-panel':
            years = meta.get('available_years', [2010, 2011, 2012])
            if years and isinstance(years[0], str):
                years = [int(y[:4]) for y in years]
            elif not years:
                years = [2010, 2011, 2012]
            panel_data = []
            for y in years:
                if _norway_classify_money_demo(meta, short_name):
                    col = _norway_demo_money_array(
                        meta, short_name, n_rows, rng, unit_ids=uids, ages=ages_vec
                    )
                    if str(meta.get('data_type', '')).lower() == 'float':
                        col = np.asarray(col, dtype=float)
                else:
                    col = rng.normal(meta.get('mean', 500000), meta.get('std', 100000), n_rows)
                df_y = pd.DataFrame({'unit_id': uids, 'tid': y, var_name: col})
                panel_data.append(df_y)
            return pd.concat(panel_data, ignore_index=True)

        result_df = pd.DataFrame(data)
        if cmd == 'import':
            _fraction = _SPARSE_FRACTION.get(short_name)
            if _fraction is not None and 0 < _fraction < 1.0:
                ids = result_df[_src_id].values.astype(np.int64)
                # Deterministisk per-ID hash (Knuth multiplicative) → stabil uavhengig av importrekkefølge
                _sparse_seed = int(hashlib.md5(f"sparse_{short_name}".encode()).hexdigest()[:8], 16) % (2**32)
                h = (ids.astype(np.uint64) * np.uint64(2654435761) + np.uint64(_sparse_seed)) % np.uint64(2**32)
                keep_mask = (h < np.uint64(int(_fraction * 4294967296))).tolist()
                result_df = result_df[keep_mask].reset_index(drop=True)
        return result_df

import statsmodels.api as sm
from statsmodels.discrete.discrete_model import Probit

def calculate_gini(x):
    """Spesialfunksjon for microdata.no gini-koeffisient"""
    x = x.dropna().values
    if len(x) == 0: return None
    n = len(x)
    s = x.sum()
    if s == 0: return 0
    return (2 * np.sum(np.sort(x) * np.arange(1, n + 1)) / (n * s)) - (n + 1) / n

def calculate_iqr(x):
    """Interkvartilavstand: 75. - 25. prosentil"""
    x = x.dropna()
    if len(x) == 0: return None
    return float(x.quantile(0.75) - x.quantile(0.25))

# Statistikk-alias for aggregate/collapse (microdata.no manual)
AGG_STAT_ALIAS = {
    'median': lambda x: x.quantile(0.5),
    'semean': 'sem',
    'sebinomial': lambda x: np.sqrt(x.mean() * (1 - x.mean()) / x.count()) if x.count() > 0 else np.nan,
    'sd': 'std',
    'percent': lambda x: 100 * x.notna().sum() / len(x) if len(x) > 0 else np.nan,
    'iqr': calculate_iqr,
    'gini': calculate_gini,
}

# ── NPR (Norsk pasientregister) ──────────────────────────────────────────────
_NPR_ENTITY = 'episode_npr'

# Mapping fra variable_metadata.json «enhetstype» → intern entity-token.
# Person er default (None → 'person').
_ENHETSTYPE_TO_ENTITY = {
    'Person':                   'person',
    'Kommune':                  'kommune',
    'Jobb':                     'jobb',
    'Kjøretøy':                 'kjoretoy',
    'Kurs':                     'kurs',
    'Trafikkulykke':            'trafikkulykke',
    'Person i trafikkulykke':   'person_i_trafikkulykke',
    'Behandlingsopphold':       _NPR_ENTITY,   # npr-alias
    'Målepunkt':                'malepunkt',
}

# Norsk visningsnavn for entity-typer (brukes i feilmeldinger)
_ENTITY_DISPLAY = {
    'person':                   'Person',
    'kommune':                  'Kommune',
    'jobb':                     'Jobb/arbeidsforhold',
    'kjoretoy':                 'Kjøretøy',
    'kurs':                     'Kurs',
    'trafikkulykke':            'Trafikkulykke',
    'person_i_trafikkulykke':   'Person i trafikkulykke',
    _NPR_ENTITY:                'Sykehusopphold (NPR)',
    'malepunkt':                'Målepunkt',
}

# Enhetstype → nøkkelkolonnenavn i DataFrame (fallback: 'unit_id')
_ENTITY_ID_COL = {
    'person': 'PERSONID_1',
    'jobb': 'ARBEIDSFORHOLD_ID',
    'kjoretoy': 'KJORETOY_ID',
    'kurs': 'NUDB_KURS_LOEPENR',
}

# Enhetstype → person-referansekolonne (kobler enheten tilbake til person-ID)
_ENTITY_PERSON_REF_COL = {
    'jobb':     'ARBEIDSFORHOLD_PERSON',
    'kjoretoy': 'KJORETOY_KJORETOYID_FNR',
    'kurs':     'NUDB_KURS_FNR',
    _NPR_ENTITY: 'NPRID',
}

# Multi-record profil: entity_type → (p_has, mean, max)
# p_has     = andel av populasjonen som har minst én enhet
# mean      = forventet antall enheter blant de som har noen (Poisson)
# max       = absolutt maks per person
_ENTITY_MULTI_RECORD_PROFILE = {
    'jobb':     {'p_has': 0.72, 'mean': 1.4, 'max': 5},
    'kjoretoy': {'p_has': 0.55, 'mean': 1.2, 'max': 4},
    'kurs':     {'p_has': 0.45, 'mean': 2.5, 'max': 12},
}

# Variabler som skal bruke hierarkisk NUS-kodegenerator
_NUS_GENERATOR_VARS = frozenset({'NUDB_KURS_NUS'})

# Variabler som bare finnes for en delpopulasjon.
# Nøkkel = kort variabelnavn (short_name fra variable_metadata.json).
# Verdi = andel av populasjonen som har variabelen (deterministisk utvalg via hash).
_SPARSE_FRACTION: dict = {
    # Grunnskole-karakterer (~12 % av populasjonen er i den aktuelle aldersgruppen)
    'NUDB_GS_STP_MAT': 0.12,
    'NUDB_GS_STP_NOH': 0.12,
    'NUDB_GS_STP_ENS': 0.12,
    # Studenter med Lånekassen (~20 %)
    'LAANEKASSEN_UTBETALT_STIPEND': 0.20,
    'LAANEKASSEN_SALDO_LAAN':       0.20,
    'LAANEKASSEN_UTBETALT_LAAN':    0.20,
    'INNTEKT_STUDIESTIPEND':        0.20,
    # Sosialhjelpsmottakere (~5 %)
    'SOSHJLPFDT_MOTTAK': 0.05,
    'SOSHJELP_BIDRAG':   0.05,
    'INNTEKT_SOSIAL':    0.05,
    # Barnevernstiltak (~4 %)
    'BARNEVERN_HJELPETIL': 0.04,
    'BARNEVERN_OMSORG':    0.03,
    'BARNEVERN_HJELPETIL12': 0.04,
    'BARNEVERN_OMSORG12':    0.03,
    # Alderspensjon (~18 % av populasjonen er 62+)
    'ALDPENSJ2011FDT_MOTTAK': 0.18,
    'ALDPENSJ2011FDT_GRAD':   0.18,
    # AFP (avtalefestet pensjon, ~8 %)
    'AFPO2011FDT_MOTTAK': 0.08,
    'AFPP2011FDT_MOTTAK': 0.08,
    # Uføretrygd (~10 % av yrkesaktiv alder)
    'UFOERP2011FDT_MOTTAK': 0.10,
    'UFOERP2011FDT_GRAD':   0.10,
    # Foreldrepenger (~4 % har nylig fått barn)
    'INNTEKT_FORELDREPENGER': 0.04,
    # Dagpenger/arbeidsledighetstrygd (~5 %)
    'INNTEKT_ARBEIDSLEDIGHETSTRYGD': 0.05,
    # Arbeidssøker-/tiltaks-status (bare personer med NAV-oppfølging etter §14a, ~4 %)
    'ARBEIDSSOKER_TILTAK': 0.04,
    # Utdanningsår-variabler (ikke alle fullfører hvert nivå)
    'NUDB_AAR_FORSTE_FULLF_BACH': 0.35,
    'NUDB_AAR_FORSTE_FULLF_HOY':  0.15,
    'NUDB_AAR_FORSTE_REG_UH':     0.40,
    # Bostøtte (~5 %)
    'BOSTOTTE_SUM_BOSTOTTE': 0.05,
    # Etterlattepensjon (sjelden)
    'ETLATEKT2011FDT_MOTTAK': 0.02,
    'ETLATBRN2011FDT_MOTTAK': 0.01,
    # Grunnstønad/hjelpestønad (sjelden)
    'GRUNNSTFDT_MOTTAK': 0.03,
    'HJELPSTFDT_MOTTAK': 0.02,
}

# FNR-referansevariabler: verdiene skal være faktiske PERSONID_1-er i datasettet,
# ikke tilfeldige tall.  generate() sampler fra eksisterende uids for disse.
_PERSONID_REF_VARS = frozenset({
    'BEFOLKNING_EKT_FNR', 'BEFOLKNING_SAMB_FNR',
    'BEFOLKNING_FAR_FNR', 'BEFOLKNING_MOR_FNR',
    'BEFOLKNING_FARFAR_FNR', 'BEFOLKNING_FARMOR_FNR',
    'BEFOLKNING_MORFAR_FNR', 'BEFOLKNING_MORMOR_FNR',
    'BEFOLKNING_SOESKEN_FNR',
    'BEFOLKNING_KONTAKT_HUSHNR', 'BEFOLKNING_KONTAKT_REGSTAT_FAMNR',
    'NUDB_KURS_FNR',
    'BEFOLKNING_MRK_FNR', 'BEFOLKNING_STATUSKODE_FNR_SAMORD',
    'ELHUB_PERS_MALEPUNKTID_FNR', 'KJORETOY_KJORETOYID_FNR',
    'TRAFULYK_PERS_FNR',
    'ARBEIDSFORHOLD_PERSON',
})

# Hendelses-profil for import-event variabler.
# ALLE rater er PER ÅR — skaleres automatisk til observasjonsvinduets lengde.
#
# p_annual       = andel av populasjonen som starter minst én ny hendelse per år
# mean_annual    = forventet antall hendelser per år blant de som har noen (Poisson)
# max            = absolutt maks per person uansett periodelengte
# duration_frac  = typisk varighet som brøk av ett år (0.10 = ~5 uker)
_EVENT_PROFILE: dict = {
    # ── Sivilstand: giftemål, skilsmisse, dødsfall i par ──────────────────────
    'SIVSTANDFDT_SIVSTAND': {
        'p_annual': 0.03, 'mean_annual': 0.4, 'max': 4, 'duration_frac': 0.01},

    # ── Barnetrygd: utbetaling per barn, nye barn starter ny periode ──────────
    'BARNETRMOTFDT_MOTTAK':  {'p_annual': 0.04, 'mean_annual': 1.0, 'max': 6,  'duration_frac': 0.25},
    'BARNETRMOTFDT_BELOP':   {'p_annual': 0.04, 'mean_annual': 1.0, 'max': 6,  'duration_frac': 0.25},
    'BARNETRMOTFDT_STATUSK': {'p_annual': 0.04, 'mean_annual': 1.0, 'max': 6,  'duration_frac': 0.25},
    'BARNETRMOTFDT_ANTBARN': {'p_annual': 0.04, 'mean_annual': 1.0, 'max': 6,  'duration_frac': 0.25},

    # ── Dagpenger / arbeidssøker ───────────────────────────────────────────────
    'ARBSOEK1992FDT_STONAD':  {'p_annual': 0.05, 'mean_annual': 1.5, 'max': 8,  'duration_frac': 0.20},
    'ARBSOEK1992FDT_TILTAK3': {'p_annual': 0.04, 'mean_annual': 1.2, 'max': 6,  'duration_frac': 0.15},
    'ARBSOEK1992FDT_TILTAK5': {'p_annual': 0.04, 'mean_annual': 1.2, 'max': 6,  'duration_frac': 0.15},
    'ARBSOEK2001FDT_HOVED':   {'p_annual': 0.05, 'mean_annual': 1.5, 'max': 8,  'duration_frac': 0.20},

    # ── Sosialhjelp: månedlige utbetalinger = mange korte perioder ─────────────
    'SOSHJLPFDT_MOTTAK': {'p_annual': 0.03, 'mean_annual': 5.5, 'max': 20, 'duration_frac': 0.07},

    # ── Rehabiliteringspenger (nærmeste vi kommer sykemeldinger) ───────────────
    'REHABFDT_MOTTAK':   {'p_annual': 0.03, 'mean_annual': 1.8, 'max': 10, 'duration_frac': 0.12},
    'REHABFDT_DAGSATS':  {'p_annual': 0.03, 'mean_annual': 1.8, 'max': 10, 'duration_frac': 0.12},
    'REHABFDT_INNV_GRAD':{'p_annual': 0.03, 'mean_annual': 1.8, 'max': 10, 'duration_frac': 0.12},

    # ── Arbeidsavklaringspenger (AAP): lang sammenhengende periode ─────────────
    'ARBAVKLARPFDT_MOTTAK': {'p_annual': 0.02, 'mean_annual': 0.3, 'max': 2,  'duration_frac': 0.50},

    # ── Uføretrygd: én lang periode (starter og varer livet ut) ───────────────
    'UFOERP1992FDT_MOTTAK': {'p_annual': 0.02, 'mean_annual': 0.2, 'max': 2,  'duration_frac': 0.90},
    'UFOERP2011FDT_MOTTAK': {'p_annual': 0.02, 'mean_annual': 0.2, 'max': 2,  'duration_frac': 0.90},
    'UFOERP2011FDT_GRAD':   {'p_annual': 0.02, 'mean_annual': 0.2, 'max': 2,  'duration_frac': 0.90},
    'UFOERP1992FDT_UFG':    {'p_annual': 0.02, 'mean_annual': 0.2, 'max': 2,  'duration_frac': 0.90},
    'TIDSUFOERPFDT_MOTTAK': {'p_annual': 0.015,'mean_annual': 0.4, 'max': 3,  'duration_frac': 0.40},
    'FUFOERPFDT_MOTTAK':    {'p_annual': 0.015,'mean_annual': 0.2, 'max': 2,  'duration_frac': 0.90},
    'FUFOERPFDT_UFG':       {'p_annual': 0.015,'mean_annual': 0.2, 'max': 2,  'duration_frac': 0.90},

    # ── Alderspensjon: starter ved ca 62–67, varer resten av livet ─────────────
    'ALDPENSJ2011FDT_MOTTAK': {'p_annual': 0.03, 'mean_annual': 0.2, 'max': 2,  'duration_frac': 0.90},
    'ALDPENSJ2011FDT_GRAD':   {'p_annual': 0.03, 'mean_annual': 0.2, 'max': 2,  'duration_frac': 0.90},
    'AFPO2011FDT_MOTTAK':     {'p_annual': 0.02, 'mean_annual': 0.2, 'max': 2,  'duration_frac': 0.90},
    'AFPP2011FDT_MOTTAK':     {'p_annual': 0.02, 'mean_annual': 0.2, 'max': 2,  'duration_frac': 0.90},

    # ── Enslig forsørger: én periode per barn ──────────────────────────────────
    'ENSLIGBTSFDT_MOTTAK': {'p_annual': 0.02, 'mean_annual': 0.5, 'max': 4,  'duration_frac': 0.35},
    'ENSLIGOVGFDT_MOTTAK': {'p_annual': 0.02, 'mean_annual': 0.5, 'max': 4,  'duration_frac': 0.35},
    'ENSLIGUTDFDT_MOTTAK': {'p_annual': 0.015,'mean_annual': 0.4, 'max': 3,  'duration_frac': 0.35},

    # ── Introduksjonsstønad / Kvalifiseringsstønad ─────────────────────────────
    'INTROSTFDT_MOTTAK':  {'p_annual': 0.008, 'mean_annual': 0.5, 'max': 2,  'duration_frac': 0.60},
    'KVALIFSTFDT_MOTTAK': {'p_annual': 0.005, 'mean_annual': 0.4, 'max': 3,  'duration_frac': 0.35},
    'SUPPLSTFDT_MOTTAK':  {'p_annual': 0.005, 'mean_annual': 0.2, 'max': 2,  'duration_frac': 0.80},
}


def _generate_event_rows(short_name, var_name, uids, src_id_col, date1_str, date2_str,
                         meta, rng):
    """Generer hendelsesrader (0+ per person) basert på _EVENT_PROFILE og metadata.

    Rater i _EVENT_PROFILE er per år. Funksjonen skalerer til faktisk periodelengde:
      p_period    = 1 - (1 - p_annual) ** n_years
      mean_period = mean_annual * n_years  (capped av max)
    """
    profile = _EVENT_PROFILE.get(short_name, {
        'p_annual': 0.10, 'mean_annual': 1.5, 'max': 6, 'duration_frac': 0.15})
    p_annual   = profile['p_annual']
    mean_ann   = profile['mean_annual']
    max_events = profile['max']
    dur_frac   = profile.get('duration_frac', 0.15)

    date1_dt  = pd.Timestamp(date1_str)
    date2_dt  = pd.Timestamp(date2_str)
    span_days = max(1, (date2_dt - date1_dt).days)
    n_years   = span_days / 365.25

    # Skaler rater til observasjonsvinduet
    p_period    = 1.0 - (1.0 - p_annual) ** n_years
    mean_period = min(max_events, mean_ann * n_years)
    avg_dur     = max(1, int(span_days * dur_frac))

    n_persons = len(uids)

    # Hvem har minst én hendelse i perioden?
    has_event    = rng.random(n_persons) < p_period
    event_uids   = uids[has_event]
    n_ev_persons = int(has_event.sum())

    if n_ev_persons == 0:
        return pd.DataFrame(columns=[src_id_col,
                                     f'START@{var_name}', f'STOP@{var_name}', var_name])

    # Antall hendelser per person (Poisson, minst 1, maks max_events)
    counts = np.clip(rng.poisson(max(0.1, mean_period), n_ev_persons), 1, max_events)
    total  = int(counts.sum())

    # Pre-cast counts til int (np.intp) FØR np.repeat for å unngå at np.repeat
    # internt prøver å safe-caste int64→int32 i Pyodide/WASM32 (samme årsak som NPR-buggen).
    repeated_uids = np.repeat(event_uids, counts.astype(int)).tolist()

    # Tilfeldige start- og sluttdatoer innenfor vinduet
    start_off = rng.integers(0, span_days, total, dtype=np.int64)
    durations = rng.integers(1, max(2, avg_dur * 2), total, dtype=np.int64)
    end_off   = np.minimum(start_off + durations, span_days)

    # Vektorisert datokonvertering
    base_epoch = int((date1_dt - pd.Timestamp('1970-01-01')).days)
    starts = (base_epoch + start_off).tolist()   # heltall: dager siden 1970-01-01
    stops  = (base_epoch + end_off).tolist()

    # Hendelsesverdi fra metadata (distribution eller labels).
    # Alltid strenger for å unngå int-dtype-konflikt ved DataFrame-konsolidering.
    meta_ev = meta or {}
    if meta_ev.get('distribution'):
        codes, probs = _normalize_distribution_weights(meta_ev['distribution'])
        raw_vals = rng.choice(codes, size=total, p=probs).tolist() if codes \
                   else rng.integers(1, 4, total, dtype=np.int64).tolist()
    else:
        lev = meta_ev.get('labels', meta_ev.get('labels_dict'))
        codes = list(lev.keys()) if isinstance(lev, dict) and lev else ['1', '2', '3']
        raw_vals = rng.choice(codes, size=total).tolist()
    vals = [str(v) for v in raw_vals]

    result = pd.DataFrame({
        src_id_col:          np.array(repeated_uids, dtype=np.int64),
        f'START@{var_name}': starts,
        f'STOP@{var_name}':  stops,
        var_name:            vals,
    })
    return result.sort_values([src_id_col, f'START@{var_name}']).reset_index(drop=True)


def _get_df_key_col(df):
    """Returnerer nøkkelkolonnen for en DataFrame, eller None."""
    if df is None:
        return None
    for c in ('PERSONID_1', 'ARBEIDSFORHOLD_ID', 'KJORETOY_ID',
              'NUDB_KURS_LOEPENR', 'AGGRSHOPPID', 'unit_id'):
        if c in df.columns:
            return c
    return None

# ICD-10: (kode, norsk_label, min_alder, max_alder, kjønn_bias, base_vekt)
# kjønn_bias: >0 = mer vanlig hos kvinner, <0 = mer vanlig hos menn
_ICD10_CODES = [
    ('I21',  'Akutt hjerteinfarkt',                              50, 95, -0.4, 6.0),
    ('I63',  'Hjerneinfarkt',                                    55, 95, -0.1, 4.0),
    ('I50',  'Hjertesvikt',                                      60, 95,  0.0, 3.5),
    ('I10',  'Essensiell hypertensjon',                          45, 90,  0.1, 3.0),
    ('I48',  'Atrieflimmer og atrieflutter',                     55, 90, -0.1, 3.0),
    ('J18',  'Pneumoni, uspesifisert',                           60, 95,  0.0, 4.0),
    ('J44',  'Annen kronisk obstruktiv lungesykdom',             50, 90, -0.1, 2.5),
    ('J45',  'Astma',                                             5, 50,  0.2, 2.0),
    ('K80',  'Cholelithiasis (gallstein)',                       35, 80,  0.4, 3.0),
    ('K35',  'Akutt appendisitt',                                 5, 40,  0.0, 2.5),
    ('K57',  'Divertikuløs sykdom i tykktarmen',                55, 90,  0.1, 2.0),
    ('K92',  'GI-blødning, uspesifisert',                       50, 90, -0.1, 2.0),
    ('S72',  'Brudd på lårhals',                                 70, 95,  0.5, 3.5),
    ('S06',  'Intrakraniell skade',                               5, 50, -0.2, 2.0),
    ('C34',  'Ondartet svulst i bronkie og lunge',               55, 85, -0.2, 2.5),
    ('C50',  'Ondartet svulst i bryst',                          35, 80,  1.0, 3.0),
    ('C18',  'Ondartet svulst i tykktarm',                       55, 85,  0.0, 2.0),
    ('F32',  'Depressiv episode',                                20, 65,  0.4, 2.5),
    ('F20',  'Schizofreni',                                      18, 55, -0.1, 1.0),
    ('O80',  'Enkelt spontant forløsning',                       18, 45,  2.0, 4.0),
    ('N20',  'Nyresten',                                         25, 65, -0.2, 2.0),
    ('N18',  'Kronisk nyresykdom',                               50, 90,  0.1, 2.0),
    ('E11',  'Diabetes mellitus type 2',                         40, 85,  0.0, 2.0),
    ('A41',  'Sepsis, uspesifisert',                             60, 95,  0.0, 2.5),
    ('G45',  'Forbigående cerebral iskemi',                      55, 85,  0.0, 1.5),
    ('G20',  'Parkinsons sykdom',                                60, 90, -0.1, 1.0),
    ('M16',  'Koksartrose (hofteleddsartrose)',                  55, 85,  0.3, 2.0),
    ('Z00',  'Allmenn helseundersøkelse',                         0, 99,  0.1, 1.5),
]
_NPR_ICD10_LABELS   = {code: label for code, label, *_ in _ICD10_CODES}
_NPR_OMSORG_LABELS  = {'døgn': 'Døgnopphold', 'dag': 'Dagopphold', 'poliklinisk': 'Poliklinisk konsultasjon'}
_NPR_OMSORG_DIST    = {'døgn': 0.60, 'dag': 0.25, 'poliklinisk': 0.15}
_NPR_NIVA_LABELS    = {'I': 'ISF-opphold', 'U': 'Utenfor ISF', 'R': 'Rehabilitering'}
_NPR_NIVA_DIST      = {'I': 0.65, 'U': 0.25, 'R': 0.10}

# Legg NPR-variabler inn i _DEMO_FALLBACK_META (brukes når katalog mangler labels)
_DEMO_FALLBACK_META.update({
    'AGGRSHOPPID':    {'entity_type': _NPR_ENTITY, 'data_type': 'int'},
    'NPRID':          {'entity_type': _NPR_ENTITY, 'data_type': 'int'},
    'HOVEDTILSTAND1': {'entity_type': _NPR_ENTITY, 'data_type': 'string',
                       'labels': _NPR_ICD10_LABELS},
    'HOVEDTILSTAND2': {'entity_type': _NPR_ENTITY, 'data_type': 'string',
                       'labels': _NPR_ICD10_LABELS},
    'INNDATO':        {'entity_type': _NPR_ENTITY, 'data_type': 'int'},
    'INNTID':         {'entity_type': _NPR_ENTITY, 'data_type': 'string'},
    'NIVA':           {'entity_type': _NPR_ENTITY, 'data_type': 'string',
                       'labels': _NPR_NIVA_LABELS, 'distribution': _NPR_NIVA_DIST},
    'OMSORGSNIVA':    {'entity_type': _NPR_ENTITY, 'data_type': 'string',
                       'labels': _NPR_OMSORG_LABELS, 'distribution': _NPR_OMSORG_DIST},
    'UTDATO':         {'entity_type': _NPR_ENTITY, 'data_type': 'int'},
    'UTTID':          {'entity_type': _NPR_ENTITY, 'data_type': 'string'},
})

class DataTransformHandler:
    """Håndterer rename, replace, drop, keep, clone-variables, destring, recode."""

    def __init__(self, label_manager=None):
        self.label_manager = label_manager

    def execute(self, cmd, df, args, options):
        cond = options.get('_condition')  # Linjenivå if (f.eks. replace x = 1 if y, drop if z)

        if cmd == 'rename':
            old, new = args['old'], args['new']
            if old in df.columns:
                df.rename(columns={old: new}, inplace=True)
            return None

        if cmd == 'replace':
            target, expr = args['target'], args['expression']
            if target not in df.columns:
                df[target] = np.nan
            m = re.match(r'^(\d+)\s+if\s+(.+)$', expr.strip())
            row_mask = _line_condition_mask(df, cond, options) if cond else None
            if row_mask is None:
                row_mask = slice(None)
            if m:
                val, c = int(m.group(1)), m.group(2)
                mask = _py_eval_cond(df, c)
                if cond:
                    mask = mask & row_mask
                df.loc[mask, target] = val
            else:
                if cond:
                    sub = df.loc[row_mask]
                    df.loc[row_mask, target] = _py_eval_expr(sub, expr)
                else:
                    df[target] = _py_eval_expr(df, expr)
            return None

        if cmd == 'drop':
            if args['mode'] == 'if':
                c = args['condition']
                mask = _line_condition_mask(df, c, options)
                return df.loc[~mask].copy()
            # Linje-nivå «… if betingelse» (parse_line setter tom remainder etter «keep/drop»):
            # da er mode «vars» men _condition er satt — filtrer rader som drop if.
            line_cond = cond
            if line_cond:
                mask = _line_condition_mask(df, line_cond, options)
                return df.loc[~mask].copy()
            cols = [v for v in args['vars'] if v in df.columns]
            return df.drop(columns=cols)

        if cmd == 'keep':
            if args['mode'] == 'if':
                c = args['condition']
                mask = _line_condition_mask(df, c, options)
                return df.loc[mask].copy()
            # Linje-nivå «keep if betingelse» → _condition satt, remainder etter «keep» ofte tom.
            line_cond = cond
            if line_cond:
                mask = _line_condition_mask(df, line_cond, options)
                sub = df.loc[mask].copy()
                cols = [v for v in args.get('vars', []) if v in sub.columns]
                if not cols:
                    return sub
                others = [c for c in sub.columns if c not in cols]
                return sub.drop(columns=others)
            cols = [v for v in args['vars'] if v in df.columns]
            others = [c for c in df.columns if c not in cols]
            return df.drop(columns=others)

        if cmd == 'clone-variables':
            prefix = options.get('prefix', '') or ''
            suffix = options.get('suffix', '') or ''
            for old, new in args['pairs']:
                if old in df.columns:
                    if prefix or suffix:
                        # prefix/suffix overstyrer -> mapping og _clone suffiks
                        actual_new = f"{prefix}{old}{suffix}"
                    else:
                        actual_new = new
                    df[actual_new] = df[old].copy()
            return None

        if cmd == 'destring':
            ignore_chars = options.get('ignore', '') or ''
            force = bool(options.get('force'))
            dpcomma = bool(options.get('dpcomma'))
            prefix = options.get('prefix', '') or ''
            suffix = options.get('suffix', '') or ''
            for v in args['vars']:
                if v not in df.columns:
                    continue
                src = df[v].astype(str)
                if dpcomma:
                    # Erstatt desimalkomma med punktum
                    src = src.str.replace(',', '.', regex=False)
                for ch in str(ignore_chars):
                    src = src.str.replace(ch, '', regex=False)
                converted = pd.to_numeric(src, errors='coerce' if force else 'coerce')
                new_col = f"{prefix}{v}{suffix}"
                df[new_col] = converted
            return None

        if cmd == 'reshape-to-panel':
            prefixes = args.get('prefixes', [])
            if not prefixes:
                return None
            id_col = _get_df_key_col(df) or df.index.name or 'id'
            id_col = id_col if id_col in df.columns else df.columns[0]
            stub_cols = {}
            time_vals = set()
            for col in df.columns:
                for pre in prefixes:
                    if col.startswith(pre) and col != pre:
                        suf = col[len(pre):]
                        # microdata: "Kun sifre og spesialtegn som ikke er bokstaver godtas som suffiks"
                        if suf and all(not c.isalpha() for c in suf):
                            stub_cols.setdefault(pre, []).append((col, suf))
                            time_vals.add(suf)
            if not stub_cols:
                return None
            time_vals = sorted(time_vals)
            rows = []
            for _, row in df.iterrows():
                for t in time_vals:
                    r = {id_col: row.get(id_col, row.name)}
                    r['tid'] = t
                    r['date@panel'] = t  # microdata.no hjelpevariabel
                    for pre, cols in stub_cols.items():
                        for full, suf in cols:
                            if suf == t:
                                r[pre] = row.get(full, np.nan)
                    for c in df.columns:
                        if c not in [x for pcols in stub_cols.values() for x, _ in pcols]:
                            r[c] = row.get(c, np.nan)
                    rows.append(r)
            return pd.DataFrame(rows)

        if cmd == 'reshape-from-panel':
            if 'tid' not in df.columns:
                return None
            id_col = _get_df_key_col(df) or df.columns[0]
            wide = df.pivot_table(index=id_col, columns='tid', aggfunc='first')
            wide.columns = [f'{a}{b}' if b else str(a) for a, b in wide.columns]
            return wide.reset_index()

        if cmd == 'recode':
            # Samle opp nye labels per variabel når regler har tekstetiketter
            new_labels_per_var = {}
            for var in args['vars']:
                if var not in df.columns:
                    continue
                # Viktig: intervaller bruker >= / <= — object-strenger ("47") matcher ikke 45–47.
                was_string = df[var].dtype == object or pd.api.types.is_string_dtype(df[var])
                df[var] = pd.to_numeric(df[var], errors='coerce')
                for rule in args['rules']:
                    rule = rule.strip()
                    if '=' not in rule:
                        continue
                    # Frisk kolonnereferanse etter hver regel (trygt ved kjedeoppdateringer)
                    col = df[var]
                    lhs, rhs = rule.split('=', 1)
                    rhs = rhs.strip()
                    # Word/Excel kan lime inn typografiske anførsel — normaliser til ASCII for regex
                    rhs = (
                        rhs.replace('\u2018', "'")
                        .replace('\u2019', "'")
                        .replace('\u201c', '"')
                        .replace('\u201d', '"')
                    )
                    # RHS kan være "kode", kode med dobbeltanførsel-etikett eller enkeltanførsel (microdata.no-stil)
                    label_text = None
                    m_val = re.match(
                        r'^([+-]?\d+(?:\.\d+)?)(?:\s+(?:"([^"]*)"|\'([^\']*)\'))?$',
                        rhs,
                    )
                    if m_val:
                        code_str = m_val.group(1)
                        label_text = m_val.group(2) if m_val.group(2) is not None else m_val.group(3)
                        try:
                            new_val = int(code_str) if '.' not in code_str else float(code_str)
                        except ValueError:
                            new_val = code_str
                    else:
                        # Fallback: gammel oppførsel, eller typografiske tegn som brøt hoved-regex
                        try:
                            new_val = int(rhs) if rhs.isdigit() or (rhs.startswith('-') and rhs[1:].isdigit()) else float(rhs)
                        except ValueError:
                            m_loose = re.match(
                                r'^([+-]?\d+(?:\.\d+)?)\s+(?:"([^"]*)"|\'([^\']*)\')',
                                rhs,
                            )
                            if m_loose:
                                code_str = m_loose.group(1)
                                label_text = m_loose.group(2) if m_loose.group(2) is not None else m_loose.group(3)
                                try:
                                    new_val = int(code_str) if '.' not in code_str else float(code_str)
                                except ValueError:
                                    new_val = code_str
                            else:
                                new_val = rhs
                    lhs = lhs.strip()
                    # Sjekk for 'miss' (missing-verdi) i lhs
                    if re.fullmatch(r'miss(?:ing)?', lhs, re.IGNORECASE):
                        mask = col.isna()
                        if hasattr(mask, 'fillna'):
                            mask = mask.fillna(False)
                        df.loc[mask, var] = new_val
                        if label_text is not None and isinstance(new_val, (int, float)):
                            if self.label_manager is not None:
                                d = new_labels_per_var.setdefault(var, {})
                                d[int(new_val)] = label_text
                        continue
                    # Tokeniser på whitespace først; hvert token kan være én verdi eller en range (lo/hi).
                    # Støtter mixed list+range, f.eks. "1 2 3 5/10".
                    vals = set()
                    ranges = []  # liste av (lo_val, hi_val)
                    for tok in lhs.split():
                        if '/' in tok:
                            parts = [p.strip() for p in tok.split('/') if p.strip()]
                            if len(parts) == 2:
                                lo, hi = parts[0], parts[1]
                                lo_val = col.min() if lo.lower() == 'min' else (col.max() if lo.lower() == 'max' else (int(lo) if lo.lstrip('-').isdigit() else float(lo)))
                                hi_val = col.max() if hi.lower() == 'max' else (col.min() if hi.lower() == 'min' else (int(hi) if hi.lstrip('-').isdigit() else float(hi)))
                                ranges.append((lo_val, hi_val))
                            else:
                                # Legacy: "1/2/3" tolkes som liste av verdier
                                for p in parts:
                                    pl = p.lower()
                                    if pl == 'min':
                                        vals.add(col.min())
                                    elif pl == 'max':
                                        vals.add(col.max())
                                    elif p.lstrip('-').isdigit():
                                        vals.add(int(p))
                                    else:
                                        try:
                                            vals.add(float(p))
                                        except ValueError:
                                            pass
                        else:
                            tl = tok.lower()
                            if tl == 'min':
                                vals.add(col.min())
                            elif tl == 'max':
                                vals.add(col.max())
                            elif tok.isdigit() or (tok.startswith('-') and tok[1:].isdigit()):
                                vals.add(int(tok))
                            else:
                                try:
                                    vals.add(float(tok))
                                except ValueError:
                                    pass
                    mask = col.isin(vals)
                    for lo_val, hi_val in ranges:
                        mask = mask | ((col >= lo_val) & (col <= hi_val))
                    if hasattr(mask, 'fillna'):
                        mask = mask.fillna(False)
                    df.loc[mask, var] = new_val
                    # Hvis vi har labeltekst og new_val er en kode, samle opp for labels
                    if label_text is not None and isinstance(new_val, (int, float)):
                        if self.label_manager is not None:
                            d = new_labels_per_var.setdefault(var, {})
                            d[int(new_val)] = label_text
                # Hele tall etter recode → nullable int (bedre tabulate/etiketter; unngår 8.0 vs 8)
                s = df[var]
                if pd.api.types.is_numeric_dtype(s):
                    sub = s.dropna()
                    if len(sub):
                        arr = sub.to_numpy(dtype=float, copy=False)
                        if np.all(np.isfinite(arr)) and np.all(arr == np.round(arr)):
                            df[var] = s.round().astype('Int64')
                # Bevar string-dtype: var variabelen strenger FØR recode, konverter tilbake.
                # Dette sikrer at f.eks. parstatus == '1' virker etter recode.
                if was_string:
                    df[var] = df[var].apply(
                        lambda x: str(int(x)) if pd.notna(x) else None
                    ).astype(object)
            # Etter at alle regler er brukt, oppdater LabelManager med nye labels
            if self.label_manager is not None and new_labels_per_var:
                for var, mapping in new_labels_per_var.items():
                    pairs = list(mapping.items())
                    codelist_name = f"{var}_recode"
                    self.label_manager.define_labels(codelist_name, pairs)
                    self.label_manager.assign_labels(var, codelist_name)
            return None

        return None


class LabelManager:
    """Håndterer define-labels, assign-labels, drop-labels, list-labels."""

    def __init__(self, catalog=None):
        self.codelists = {}  # codelist_name -> {value: label}
        self.var_to_codelist = {}  # var_name -> codelist_name
        self.var_alias_to_path = {}  # kolonnenavn/alias -> variabel NAME (for automatisk labels fra metadata)
        self.catalog = catalog or {}
        self._catalog_by_short = {k.split('/')[-1]: v for k, v in self.catalog.items()}
        self._load_from_catalog()

    def register_var_alias(self, alias, var_path):
        """Registrer at kolonnenavn alias kommer fra variabel var_path (f.eks. bosted <- db/BEFOLKNING_KOMMNR_FAKTISK). Lagrer kun NAME for catalog-oppslag."""
        if alias and var_path:
            name = var_path.split('/')[-1]
            self.var_alias_to_path[alias] = name

    @staticmethod
    def _label_key_to_int(k):
        """Konverter label-nøkkel til int der mulig (0301 -> 301, -1 -> -1), ellers behold."""
        try:
            return int(k)
        except (ValueError, TypeError):
            return k

    def _load_from_catalog(self):
        """Pre-define codelists fra variable_metadata.json (labels eller codelist-felt)."""
        for var_name, meta in self.catalog.items():
            short = var_name.split('/')[-1]
            labels = meta.get('labels', meta.get('labels_dict'))
            if isinstance(labels, dict):
                cname = meta.get('codelist', f"{short}_labels")
                if cname not in self.codelists:
                    mapping = {self._label_key_to_int(k): v for k, v in labels.items()}
                    self.codelists[cname] = mapping

    def refresh_after_catalog_mutation(self):
        """Kall etter lazy innlasting av external_metadata: oppdater short-index og codelists for nye labels."""
        self._catalog_by_short = {k.split('/')[-1]: v for k, v in self.catalog.items()}
        for var_name, meta in self.catalog.items():
            if not isinstance(meta, dict):
                continue
            labels = meta.get('labels', meta.get('labels_dict'))
            if not isinstance(labels, dict) or not labels:
                continue
            short = var_name.split('/')[-1]
            cname = meta.get('codelist', f"{short}_labels")
            self.codelists[cname] = {self._label_key_to_int(k): v for k, v in labels.items()}

    def define_labels(self, name, pairs):
        """pairs: [(value, label), ...]"""
        mapping = {}
        for val, label in pairs:
            mapping[val] = label
        self.codelists[name] = mapping

    def assign_labels(self, var_name, codelist_name):
        if codelist_name not in self.codelists:
            raise ValueError(f"Kodeliste '{codelist_name}' er ikke definert. Bruk define-labels først.")
        self.var_to_codelist[var_name] = codelist_name

    def drop_labels(self, *names):
        for n in names:
            self.codelists.pop(n, None)
            to_remove = [v for v, c in self.var_to_codelist.items() if c == n]
            for v in to_remove:
                del self.var_to_codelist[v]

    def get_codelist_for_var(self, var_name, time=None):
        """Returnerer codelist-dict for variabel, eller None.

        Prioritet:
        1) Eksplisitt assign-labels
        2) Metadata for alias (fra import)
        3) Katalog på var_name/short
        4) Felles kommune-kodeliste (BOSATTEFDT_BOSTED / BOSATT_KOMMUNE) for kommunevariabler
        """
        cname = self.var_to_codelist.get(var_name)
        if cname:
            return self.codelists.get(cname)
        path = self.var_alias_to_path.get(var_name)
        if path:
            meta = self.catalog.get(path)
            if meta:
                labels = meta.get('labels', meta.get('labels_dict'))
                # Tom {} fra metadata: ikke returner tom codelist — fall tilbake til felles kommune-liste
                if isinstance(labels, dict) and len(labels) > 0:
                    return {self._label_key_to_int(k): v for k, v in labels.items()}
        meta = self.catalog.get(var_name) or self._catalog_by_short.get(var_name.split('/')[-1] if '/' in str(var_name) else var_name)
        if meta:
            labels = meta.get('labels', meta.get('labels_dict'))
            if isinstance(labels, dict) and len(labels) > 0:
                return {self._label_key_to_int(k): v for k, v in labels.items()}
        # Fallback: kommunevariabler uten egne labels får kodelisten fra felles kommunevariabel.
        # Vi sjekker både alias-path (f.eks. bosted <- BEFOLKNING_KOMMNR_FORMELL) og selve var_name.
        commune_sources = {
            'BEFOLKNING_KOMMNR_FORMELL',
            'BEFOLKNING_KOMMNR_FAKTISK',
            'BOSATT_KOMMUNE',
            'BOSATTEFDT_BOSTED',
            'KOMMNR_FORMELL',
            'KOMMNR_FAKTISK',
        }
        source_name = self.var_alias_to_path.get(var_name, var_name)
        short_source = source_name.split('/')[-1] if '/' in str(source_name) else source_name
        if short_source in commune_sources:
            for base_name in ('BOSATTEFDT_BOSTED', 'BOSATT_KOMMUNE'):
                base_meta = self.catalog.get(base_name)
                if base_meta:
                    labels = base_meta.get('labels', base_meta.get('labels_dict'))
                    if isinstance(labels, dict) and len(labels) > 0:
                        return {self._label_key_to_int(k): v for k, v in labels.items()}
            # Siste utvei (samme som MockDataEngine-minimal)
            ml = _MINIMAL_KOMMUNE_BASE.get('labels')
            if isinstance(ml, dict) and ml:
                return {self._label_key_to_int(k): v for k, v in ml.items()}
        return None

    @staticmethod
    def _var_allows_fylke_padding(var_name):
        """True bare for kommune-/fylkesvariabler der '03'-nøkler er meningsfulle (ikke yrkeskoder 3 → '03')."""
        if not var_name:
            return False
        commune_sources = {
            'BEFOLKNING_KOMMNR_FORMELL',
            'BEFOLKNING_KOMMNR_FAKTISK',
            'BOSATT_KOMMUNE',
            'BOSATTEFDT_BOSTED',
            'KOMMNR_FORMELL',
            'KOMMNR_FAKTISK',
        }
        s = str(var_name)
        short = s.split('/')[-1] if '/' in s else s
        return short in commune_sources or s in commune_sources

    def _lookup_label_in_codelist(self, cl, v, var_name=None):
        """Returnerer etikettstreng hvis v matcher en nøkkel i cl, ellers None."""
        if not cl:
            return None
        try:
            if pd.isna(v):
                return None
        except (ValueError, TypeError):
            pass
        x = v
        if hasattr(x, 'item') and isinstance(x, (np.integer, np.floating, np.bool_)):
            try:
                x = x.item()
            except Exception:
                pass
        if isinstance(x, np.bool_):
            x = bool(x)
        if x in cl:
            return cl[x]
        try:
            if isinstance(x, (float, np.floating)) and not isinstance(x, bool):
                if math.isfinite(float(x)) and float(x) == int(x):
                    iv = int(x)
                    if iv in cl:
                        return cl[iv]
            if isinstance(x, (int, np.integer)) and not isinstance(x, bool):
                iv = int(x)
                if iv in cl:
                    return cl[iv]
                # Kun for kommune/fylke: prøv '01'..'99' — ikke for andre kodelister (yrke, næring, …)
                if self._var_allows_fylke_padding(var_name) and 0 <= iv <= 99:
                    sk = f"{iv:02d}"
                    if sk in cl:
                        return cl[sk]
            if isinstance(x, str):
                s = x.strip()
                if s.lstrip('-').isdigit():
                    iv = int(s)
                    if iv in cl:
                        return cl[iv]
                try:
                    fx = float(s)
                    if math.isfinite(fx) and fx == int(fx):
                        iv = int(fx)
                        if iv in cl:
                            return cl[iv]
                except ValueError:
                    pass
                if s in cl:
                    return cl[s]
        except (ValueError, TypeError, OverflowError):
            pass
        return None

    def format_value(self, var_name, value):
        """Returnerer label for verdi, eller råverdi hvis ingen label."""
        if pd.isna(value):
            return value
        cl = self.get_codelist_for_var(var_name)
        if cl is None:
            return value
        lbl = self._lookup_label_in_codelist(cl, value, var_name)
        if lbl is not None:
            return lbl
        return value

    def apply_labels_to_series(self, series, var_name):
        """Mapper series index/values til labels. Returnerer ny Series."""
        cl = self.get_codelist_for_var(var_name)
        if not cl:
            return series
        def _lookup(v):
            if pd.isna(v):
                return v
            lbl = self._lookup_label_in_codelist(cl, v, var_name)
            if lbl is not None:
                return lbl
            try:
                sv = str(v)
                if sv in cl:
                    return cl[sv]
            except (ValueError, TypeError):
                pass
            return v
        if hasattr(series, 'index'):
            new_index = [_lookup(x) for x in series.index]
            return pd.Series(series.values, index=new_index)
        return series

    def apply_labels_to_frame(self, obj, var1, var2=None):
        """Mapper DataFrame/Series indeks og kolonner til labels."""
        cl1 = self.get_codelist_for_var(var1)
        cl2 = self.get_codelist_for_var(var2) if var2 else None
        if not cl1 and not cl2:
            return obj
        def _lookup(cl, val, vname):
            if cl is None:
                return val
            if pd.isna(val):
                return val
            lbl = self._lookup_label_in_codelist(cl, val, vname)
            if lbl is not None:
                return lbl
            try:
                sv = str(val)
                if sv in cl:
                    return cl[sv]
            except (ValueError, TypeError):
                pass
            return val
        if isinstance(obj, pd.Series):
            idx = [_lookup(cl1, x, var1) for x in obj.index] if cl1 else obj.index.tolist()
            return pd.Series(obj.values, index=idx)
        if isinstance(obj, pd.DataFrame):
            df = obj.copy()
            if cl1 and hasattr(df.index, 'tolist'):
                df.index = [_lookup(cl1, x, var1) for x in df.index]
            if cl2 and hasattr(df.columns, 'tolist'):
                df.columns = [_lookup(cl2, x, var2) for x in df.columns]
            return df
        return obj

    def list_labels_output(self, codelist_or_var, time=None):
        """Formatterer kodeliste for list-labels output."""
        cl = self.codelists.get(codelist_or_var) or self.get_codelist_for_var(codelist_or_var, time)
        if not cl:
            return f"Kodeliste eller variabel '{codelist_or_var}' ikke funnet."
        lines = [f"  {k}: {v}" for k, v in sorted(cl.items(), key=lambda x: (str(x[0]), x[0]))]
        return "Kodeliste " + codelist_or_var + ":\n" + "\n".join(lines)


class StatsEngine:
    def execute(self, cmd, df, args, options):
        if cmd == 'generate':
            expr = args['expression']
            # Rydd opp eventuelle utilsiktede linjeskift i uttrykket
            if isinstance(expr, str) and '\n' in expr:
                expr = " ".join(expr.splitlines())
            # Fiks presedens for & og | (Python: & binder sterkere enn >= osv.)
            if isinstance(expr, str) and ('&' in expr or '|' in expr):
                expr = _stata_like_bool_fixup(expr)
            line_cond = options.get('_condition')  # generate x = expr if cond => NaN der cond ikke holder
            # Oversett "1 if cond" / "0 if cond" til np.where (microdata-lignende syntaks)
            # Microdata-semantikk: der betingelsen IKKE holder → NaN (ikke komplement!)
            m = re.match(r'^(\d+)\s+if\s+(.+)$', expr.strip())
            if m:
                val, cond_expr = int(m.group(1)), m.group(2)
                expr = f"np.where({cond_expr}, {val}, np.nan)"

            # Evaluer generate-uttrykket med ren Python eval over df-kolonner og microdata-funksjoner
            evaluated = _py_eval_expr(df, expr)

            if line_cond:
                mask = _py_eval_cond(df, line_cond)
                df[args['target']] = np.where(mask, evaluated, np.nan)
            else:
                df[args['target']] = evaluated
            return None

        if cmd == 'aggregate':
            by_var = options.get('by')
            if not by_var:
                raise ValueError("aggregate krever opsjonen by()")
            for target in args['targets']:
                stat, src = target['stat'], target['src']
                new_var = target['target'] or src
                stat_fn = AGG_STAT_ALIAS.get(stat, stat)
                df[new_var] = df.groupby(by_var)[src].transform(stat_fn)
            return None

        if cmd == 'collapse':
            by_var = options.get('by')
            missing = [t['src'] for t in args['targets'] if t['src'] not in df.columns]
            if missing:
                raise ValueError(
                    f"Kolonner {missing} finnes ikke i datasettet. "
                    "collapse erstatter data med aggregert resultat; bruk én collapse med alle (stat) var -> navn i samme kommando, f.eks. collapse (mean) inntekt -> snitt (count) inntekt -> antall, by(kommune)"
                )
            agg_dict = {}
            for t in args['targets']:
                stat_fn = AGG_STAT_ALIAS.get(t['stat'], t['stat'])
                target_col = t['target'] or t['src']
                agg_dict[target_col] = (t['src'], stat_fn)
            if not by_var:
                # Global collapse: én rad med aggregert resultat
                row = {}
                for name, (src, fn) in agg_dict.items():
                    s = df[src]
                    row[name] = fn(s) if callable(fn) else s.agg(fn)
                return pd.DataFrame([row])
            return df.groupby(by_var, dropna=False).agg(**agg_dict).reset_index()

        if cmd == 'summarize':
            by_var = options.get('by')
            vars_to_sum = list(args if args else df.columns.drop(['unit_id', 'PERSONID_1'], errors='ignore'))
            vars_to_sum = [v for v in vars_to_sum if v in df.columns and pd.api.types.is_numeric_dtype(df[v])]
            if not vars_to_sum:
                return pd.DataFrame()
            if by_var and by_var in df.columns:
                # Gruppert summarize
                grp = df.groupby(by_var, dropna=False)[vars_to_sum]
                result = grp.agg(['mean', 'std', 'min', 'max', 'count'])
                if 'gini' in options:
                    for v in vars_to_sum:
                        result[(v, 'gini')] = df.groupby(by_var, dropna=False)[v].apply(calculate_gini)
                if 'iqr' in options:
                    for v in vars_to_sum:
                        result[(v, 'iqr')] = df.groupby(by_var, dropna=False)[v].apply(calculate_iqr)
                return result
            # Bygg statistikk-rader: Gj.snitt, Std.avvik, Antall, persentiler
            col_map = {}
            col_map['Gj.snitt'] = {v: df[v].mean() for v in vars_to_sum}
            col_map['Std.avvik'] = {v: df[v].std() for v in vars_to_sum}
            col_map['Antall'] = {v: df[v].count() for v in vars_to_sum}
            for pct, label in [(0.01, '1%'), (0.25, '25%'), (0.5, '50%'), (0.75, '75%'), (0.99, '99%')]:
                col_map[label] = {v: df[v].quantile(pct) for v in vars_to_sum}
            if 'gini' in options:
                col_map['Gini'] = {v: calculate_gini(df[v]) for v in vars_to_sum}
            if 'iqr' in options:
                col_map['IQR'] = {v: calculate_iqr(df[v]) for v in vars_to_sum}
            result = pd.DataFrame(col_map, index=vars_to_sum)
            return result

        if cmd == 'normaltest':
            from scipy import stats as scipy_stats
            vars_list = list(args) if args else [c for c in df.columns if c not in ('unit_id', 'PERSONID_1', 'tid') and pd.api.types.is_numeric_dtype(df[c])]
            vars_list = [v for v in vars_list if v in df.columns and pd.api.types.is_numeric_dtype(df[v])]
            if not vars_list:
                return pd.DataFrame()
            rows = []
            for var in vars_list:
                s = df[var].dropna()
                if len(s) < 3:
                    rows.append({'Variable': var, 'Test': '-', 'Statistic': np.nan, 'p-value': np.nan})
                    continue
                skew = scipy_stats.skew(s)
                kurt = scipy_stats.kurtosis(s)
                nt_stat, nt_p = scipy_stats.normaltest(s)
                jb_stat, jb_p = scipy_stats.jarque_bera(s)
                sw_stat, sw_p = (np.nan, np.nan)
                if len(s) <= 5000:
                    sw_stat, sw_p = scipy_stats.shapiro(s)
                rows.append({'Variable': var, 'Test': 'skewness', 'Statistic': skew, 'p-value': np.nan})
                rows.append({'Variable': var, 'Test': 'kurtosis', 'Statistic': kurt, 'p-value': np.nan})
                rows.append({'Variable': var, 'Test': 'normaltest (s-k)', 'Statistic': nt_stat, 'p-value': nt_p})
                rows.append({'Variable': var, 'Test': 'Jarque-Bera', 'Statistic': jb_stat, 'p-value': jb_p})
                rows.append({'Variable': var, 'Test': 'Shapiro-Wilk', 'Statistic': sw_stat, 'p-value': sw_p})
            return pd.DataFrame(rows)

        if cmd == 'correlate':
            vars_list = list(args) if args else [c for c in df.columns if c not in ('unit_id', 'PERSONID_1', 'tid') and pd.api.types.is_numeric_dtype(df[c])]
            vars_list = [v for v in vars_list if v in df.columns and pd.api.types.is_numeric_dtype(df[v])]
            if not vars_list:
                return pd.DataFrame()
            sub = df[vars_list]
            if options.get('pairwise'):
                corr = sub.corr(method='pearson')
            else:
                sub = sub.dropna()
                corr = sub.corr(method='pearson')
            if options.get('covariance'):
                corr = sub.cov()
            if options.get('sig'):
                from scipy.stats import pearsonr
                n = len(sub) if not options.get('pairwise') else None
                def _pval(a, b):
                    if options.get('pairwise'):
                        valid = ~(a.isna() | b.isna())
                        if valid.sum() < 3: return np.nan
                        r, p = pearsonr(a[valid], b[valid])
                        return p
                    r, p = pearsonr(a, b)
                    return p
                pvals = pd.DataFrame(index=corr.index, columns=corr.columns)
                for i, c1 in enumerate(vars_list):
                    for j, c2 in enumerate(vars_list):
                        pvals.loc[c1, c2] = _pval(sub[c1], sub[c2])
                # Formatert teksttabell med faste bredder (overskrift og spacing lesbart)
                w_label = max(len(v) for v in vars_list)
                w_label = max(w_label, 12)
                w_cell = 18
                def cell_str(r, p):
                    if pd.isna(r) or pd.isna(p):
                        return ''
                    return f'{float(r): .4f} (p={float(p):.4f})'.ljust(w_cell)
                lines = []
                header = ''.join(v.ljust(w_cell) for v in vars_list)
                lines.append(''.ljust(w_label) + header)
                for v in vars_list:
                    row_cells = [cell_str(corr.loc[v, c], pvals.loc[v, c]) for c in vars_list]
                    row_str = v.ljust(w_label) + ''.join(row_cells)
                    lines.append(row_str)
                return '\n'.join(lines)
            if options.get('obs') and not options.get('pairwise'):
                obs = sub.notna().sum()
                return pd.DataFrame({'corr': corr, 'obs': obs}) if len(vars_list) == 1 else corr
            return corr

        if cmd == 'ci':
            from scipy import stats as scipy_stats
            vars_list = list(args) if args else [c for c in df.columns if c not in ('unit_id', 'PERSONID_1', 'tid') and pd.api.types.is_numeric_dtype(df[c])]
            vars_list = [v for v in vars_list if v in df.columns and pd.api.types.is_numeric_dtype(df[v])]
            if not vars_list:
                return pd.DataFrame()
            lvl = options.get('level', 95)
            level = float(lvl) / 100 if lvl else 0.95
            rows = []
            for v in vars_list:
                s = df[v].dropna()
                n = len(s)
                mean = s.mean()
                if n < 2:
                    rows.append({'Variable': v, 'Mean': mean, 'Std Err': np.nan, 'CI_low': np.nan, 'CI_high': np.nan})
                    continue
                sem = scipy_stats.sem(s)
                t_val = scipy_stats.t.ppf((1 + level) / 2, n - 1)
                lo, hi = mean - t_val * sem, mean + t_val * sem
                rows.append({'Variable': v, 'Mean': mean, 'Std Err': sem, 'CI_low': lo, 'CI_high': hi})
            result = pd.DataFrame(rows)
            result.attrs['level'] = int(level * 100)
            return result

        if cmd == 'anova':
            if len(args) < 2:
                return pd.DataFrame()
            dep_var = args[0]
            factors = [a for a in args[1:] if a in df.columns]
            if dep_var not in df.columns or not factors:
                return pd.DataFrame()
            from statsmodels.formula.api import ols
            from statsmodels.stats.anova import anova_lm
            formula_terms = [f"C({f})" for f in factors]
            for a in args[1:]:
                if '#' in a:
                    parts = a.replace('##', '#').split('#')
                    formula_terms.append(':'.join(f"C({p.strip()})" for p in parts if p.strip() in df.columns))
            formula = f"{dep_var} ~ " + " + ".join(formula_terms)
            model = ols(formula, data=df).fit()
            anova_table = anova_lm(model, typ=2)
            return anova_table

        if cmd == 'summarize-panel':
            if 'tid' not in df.columns:
                raise ValueError("summarize-panel krever paneldata (tid-kolonne mangler).")
            vars_list = list(args) if args else [c for c in df.columns if c not in ('unit_id', 'PERSONID_1', 'tid') and pd.api.types.is_numeric_dtype(df[c])]
            vars_list = [v for v in vars_list if v in df.columns and pd.api.types.is_numeric_dtype(df[v])]
            if not vars_list:
                return pd.DataFrame()
            grp = df.groupby('tid')[vars_list]
            result = grp.agg(['mean', 'std', 'min', 'max', 'count'])
            if 'gini' in options:
                gini_by_tid = df.groupby('tid')[vars_list].apply(lambda g: g.apply(calculate_gini))
                gini_df = pd.DataFrame({('gini', v): gini_by_tid[v] for v in vars_list})
                result = pd.concat([result, gini_df], axis=1)
            if 'iqr' in options:
                iqr_by_tid = df.groupby('tid')[vars_list].apply(lambda g: g.apply(calculate_iqr))
                iqr_df = pd.DataFrame({('iqr', v): iqr_by_tid[v] for v in vars_list})
                result = pd.concat([result, iqr_df], axis=1)
            return result

        if cmd == 'tabulate':
            var1 = args[0]
            var2 = args[1] if len(args) > 1 else None
            dropna = 'missing' not in options

            if 'summarize' in options:
                # Volumtabell: summarize(var [, var2 ...]) [mean|std|sum|p50|p25|p75|gini|iqr]
                # summarize kan inneholde én eller flere komma-separerte variabler
                val_var_spec = options['summarize']
                val_vars = [v.strip() for v in str(val_var_spec).split(',') if v.strip()]
                agg_map = {'mean': 'mean', 'std': 'std', 'sum': 'sum', 'p50': lambda x: x.quantile(0.5),
                          'p25': lambda x: x.quantile(0.25), 'p75': lambda x: x.quantile(0.75),
                          'gini': calculate_gini, 'iqr': calculate_iqr}
                agg_func = 'mean'
                for k in ['p50', 'p25', 'p75', 'std', 'sum', 'gini', 'iqr']:
                    if k in options:
                        agg_func = agg_map[k]
                        break
                val_var = val_vars[0]  # første variabel for backward-compat
                if var2:
                    tb = pd.crosstab(df[var1], df[var2], values=df[val_var], aggfunc=agg_func, dropna=dropna,
                                     margins=True, margins_name='Total')
                else:
                    if len(val_vars) > 1:
                        # Flere variabler: lag kolonne per variabel
                        tb = df.groupby(var1, dropna=not dropna)[val_vars].agg(agg_func)
                        total_row = df[val_vars].agg(agg_func)
                        total_row.name = 'Total'
                        tb = pd.concat([tb, total_row.to_frame().T])
                    else:
                        tb = df.groupby(var1, dropna=not dropna)[val_var].agg(agg_func)
                        if callable(agg_func):
                            total_val = agg_func(df[val_var].dropna())
                        else:
                            total_val = getattr(df[val_var], agg_func)()
                        tb = pd.concat([tb, pd.Series([total_val], index=['Total'])])
                # top(n) / bottom(n) — bevar Total-rad/kolonne
                def _parse_n(opt_val, default=10):
                    if opt_val is True: return default
                    try: return int(opt_val)
                    except (ValueError, TypeError): return default
                if 'top' in options or 'bottom' in options:
                    # Lagre og fjern Total før slicing
                    if isinstance(tb, pd.DataFrame) and 'Total' in tb.index:
                        total_row_saved = tb.loc[['Total']]
                        total_col_saved = tb['Total'] if 'Total' in tb.columns else None
                        tb_data = tb.drop(index='Total')
                        if 'Total' in tb_data.columns:
                            tb_data = tb_data.drop(columns='Total')
                    elif isinstance(tb, pd.Series) and 'Total' in tb.index:
                        total_row_saved = tb['Total']
                        total_col_saved = None
                        tb_data = tb.drop('Total')
                    else:
                        total_row_saved = None
                        total_col_saved = None
                        tb_data = tb
                    if 'top' in options:
                        n = _parse_n(options.get('top'), 10)
                        tb_data = tb_data.head(n) if hasattr(tb_data, 'head') else tb_data.iloc[:n]
                    elif 'bottom' in options:
                        n = _parse_n(options.get('bottom'), 10)
                        tb_data = tb_data.tail(n) if hasattr(tb_data, 'tail') else tb_data.iloc[-n:]
                    # Legg tilbake Total
                    if isinstance(tb_data, pd.DataFrame) and total_row_saved is not None:
                        if total_col_saved is not None:
                            tb_data['Total'] = total_col_saved.drop('Total').reindex(tb_data.index)
                            tb_data.loc['Total'] = total_row_saved.values[0]
                        else:
                            tb_data = pd.concat([tb_data, total_row_saved])
                    elif isinstance(tb_data, pd.Series) and total_row_saved is not None:
                        tb_data = pd.concat([tb_data, pd.Series([total_row_saved], index=['Total'])])
                    tb = tb_data
                if 'flatten' in options and hasattr(tb, 'to_frame'):
                    tb = tb.to_frame().reset_index()
                lm = options.get('_label_manager')
                if lm and var2:
                    return lm.apply_labels_to_frame(tb, var1, var2)
                if lm:
                    return lm.apply_labels_to_frame(tb, var1)
                return tb

            # Frekvenstabell: rowpct, colpct, cellpct, chi2
            normalize = False
            if 'rowpct' in options: normalize = 'index'
            elif 'colpct' in options: normalize = 'columns'
            elif 'cellpct' in options: normalize = 'all'

            if var2:
                ct = pd.crosstab(df[var1], df[var2], normalize=normalize, dropna=dropna,
                                 margins=True, margins_name='Total')
                if 'chi2' in options:
                    from scipy.stats import chi2_contingency
                    ct_raw = pd.crosstab(df[var1], df[var2], dropna=dropna)
                    chi2, p, dof, exp = chi2_contingency(ct_raw)
                    ct = ct.astype(str)
                    ct.loc['_chi2'] = f'chi2={chi2:.4f}, p={p:.4f}, dof={dof}'
                if 'top' in options or 'bottom' in options:
                    # Lagre Total-rad/kolonne og chi2-rad, slice data, legg tilbake
                    chi2_row = ct.loc[['_chi2']] if '_chi2' in ct.index else None
                    total_row = ct.loc[['Total']] if 'Total' in ct.index else None
                    total_col = ct['Total'] if 'Total' in ct.columns else None
                    drop_idx = [i for i in ['Total', '_chi2'] if i in ct.index]
                    ct_data = ct.drop(index=drop_idx) if drop_idx else ct
                    if 'Total' in ct_data.columns:
                        ct_data = ct_data.drop(columns='Total')
                    if 'top' in options:
                        n = int(options.get('top', 10))
                        ct_data = ct_data.head(n)
                    else:
                        n = int(options.get('bottom', 10))
                        ct_data = ct_data.tail(n)
                    # Legg tilbake Total-kolonne og -rad
                    if total_col is not None:
                        ct_data['Total'] = total_col.reindex(ct_data.index)
                    if total_row is not None:
                        total_vals = total_row.iloc[0].reindex(ct_data.columns)
                        ct_data.loc['Total'] = total_vals
                    if chi2_row is not None:
                        chi2_vals = chi2_row.iloc[0].reindex(ct_data.columns)
                        ct_data.loc['_chi2'] = chi2_vals
                    ct = ct_data
                if 'flatten' in options:
                    ct = ct.reset_index()
                lm = options.get('_label_manager')
                if lm:
                    ct = lm.apply_labels_to_frame(ct, var1, var2)
                return ct
            else:
                vc = df[var1].value_counts(normalize=normalize, dropna=not dropna)
                total = vc.sum()
                if 'top' in options:
                    n = int(options.get('top', 10))
                    vc = vc.head(n)
                elif 'bottom' in options:
                    n = int(options.get('bottom', 10))
                    vc = vc.tail(n)
                vc = pd.concat([vc, pd.Series([total], index=['Total'])])
                lm = options.get('_label_manager')
                if lm:
                    vc = lm.apply_labels_to_series(vc, var1)
                return vc

        if cmd == 'tabulate-panel':
            if 'tid' not in df.columns:
                raise ValueError("tabulate-panel krever paneldata (tid-kolonne mangler).")
            var1 = args[0]
            vars_rest = args[1:] if len(args) > 1 else []
            dropna = 'missing' not in options
            # Variabel 1 nedover, tid kolonnevis (som var2)
            if vars_rest:
                row_idx = [var1] + list(vars_rest)
                row_vals = df[row_idx].astype(str).agg(' | '.join, axis=1)
                row_vals.name = ' x '.join(row_idx)
            else:
                row_vals = df[var1]
            ct = pd.crosstab(row_vals, df['tid'], normalize='columns' if 'colpct' in options else False, dropna=dropna)
            if 'rowpct' in options:
                ct = ct.div(ct.sum(axis=1), axis=0)
            if 'summarize' in options:
                val_var = options['summarize']
                agg_map = {'mean': 'mean', 'std': 'std', 'p50': lambda x: x.quantile(0.5)}
                agg_func = agg_map.get('p50' if 'p50' in options else 'std' if 'std' in options else 'mean', 'mean')
                ct = df.pivot_table(index=row_vals if hasattr(row_vals, 'name') else pd.Series(row_vals, index=df.index),
                                   columns='tid', values=val_var, aggfunc=agg_func)
            lm = options.get('_label_manager')
            if lm and not vars_rest:
                ct = lm.apply_labels_to_frame(ct, var1, None)
            return ct

        if cmd == 'transitions-panel':
            if 'tid' not in df.columns:
                raise ValueError("transitions-panel krever paneldata (tid-kolonne mangler).")
            _key = _get_df_key_col(df)
            if not _key:
                raise ValueError("transitions-panel krever enhetsnøkkel (PERSONID_1 eller unit_id).")
            vars_list = list(args) if args else [c for c in df.columns if c not in ('unit_id', 'PERSONID_1', 'tid')]
            vars_list = [v for v in vars_list if v in df.columns]
            if not vars_list:
                return pd.DataFrame()
            results = []
            for var in vars_list:
                df_s = df[[_key, 'tid', var]].sort_values([_key, 'tid']).dropna(subset=[var])
                df_s['_next'] = df_s.groupby(_key)[var].shift(-1)
                pairs = df_s.dropna(subset=['_next'])
                if pairs.empty:
                    results.append(pd.DataFrame())
                    continue
                ct = pd.crosstab(pairs[var], pairs['_next'], normalize='index')
                lm = options.get('_label_manager')
                if lm:
                    ct = lm.apply_labels_to_frame(ct, var, None)
                results.append(ct)
            if len(results) == 1:
                return results[0]
            return results

class RegressionHandler:
    def _add_const(self, X, add):
        return sm.add_constant(X) if add else X

    def _apply_cov(self, model, options, df_clean=None):
        """Bruk robust eller cluster standardfeil."""
        if options.get('cluster') and df_clean is not None:
            cov = options['cluster']
            try:
                return model.get_robustcov_results(cov_type='cluster', groups=df_clean[cov].values)
            except Exception:
                return model
        if options.get('robust'):
            try:
                return model.get_robustcov_results(cov_type='HC1')
            except Exception:
                return model
        return model

    def _panel_predict_extra(self, model, Y, X, panel_df, key_col, df_clean, options, alpha, model_type, g=None, Y_orig=None, X_orig=None):
        """Bygg extra dict med predicted/residuals/effects for regress-panel-predict (statsmodels fallback)."""
        extra = {}
        pred_name = options.get('predicted', 'predicted')
        res_name = options.get('residuals')
        eff_name = options.get('effects')

        if model_type == 'fe' and g is not None:
            # FE: predicted = entity_mean + X_within @ beta
            fitted_within = model.predict(X)
            entity_means = Y_orig.groupby(g).transform('mean')
            predicted = entity_means + fitted_within
            residuals = Y_orig - predicted
            if pred_name:
                extra[str(pred_name) if pred_name is not True else 'predicted'] = pd.Series(predicted.values, index=panel_df.index)
            if res_name:
                extra[str(res_name) if res_name is not True else 'residuals'] = pd.Series(residuals.values, index=panel_df.index)
            if eff_name:
                extra[str(eff_name) if eff_name is not True else 'effects'] = pd.Series(entity_means.values, index=panel_df.index)
        elif model_type == 're':
            # RE via MixedLM
            predicted = model.fittedvalues
            residuals = Y - predicted
            if pred_name:
                extra[str(pred_name) if pred_name is not True else 'predicted'] = pd.Series(predicted.values, index=panel_df.index)
            if res_name:
                extra[str(res_name) if res_name is not True else 'residuals'] = pd.Series(residuals.values, index=panel_df.index)
            if eff_name:
                try:
                    re_effects = model.random_effects
                    eff_series = panel_df[key_col].map({k: v.iloc[0] if hasattr(v, 'iloc') else v for k, v in re_effects.items()})
                    extra[str(eff_name) if eff_name is not True else 'effects'] = pd.Series(eff_series.values, index=panel_df.index)
                except Exception:
                    pass
        else:
            # pooled
            predicted = model.predict(X)
            residuals = Y - predicted
            if pred_name:
                extra[str(pred_name) if pred_name is not True else 'predicted'] = pd.Series(predicted.values, index=panel_df.index)
            if res_name:
                extra[str(res_name) if res_name is not True else 'residuals'] = pd.Series(residuals.values, index=panel_df.index)

        if not extra:
            predicted = model.predict(X) if model_type != 'fe' else (entity_means + model.predict(X))
            extra['predicted'] = pd.Series(predicted.values, index=panel_df.index)
        summary = str(model.summary(alpha=alpha)) if hasattr(model, 'summary') else str(model)
        return (summary, extra)

    def _fit_simple(self, reg_cmd, df, args, options):
        """Fit en enkel regresjon og returner (model, dep_var, indep_vars, df_clean).
        Brukes av coefplot og evt. andre metoder som trenger råmodellen.
        """
        dep_var = args[0]
        raw_indep = list(args[1:])
        add_const = 'noconstant' not in options

        # i. prefix → dummies
        factor_map = {}
        for v in raw_indep:
            if v.startswith('i.'):
                base = v[2:]
                if base not in df.columns:
                    raise ValueError(f"Faktorvariabel '{base}' finnes ikke i datasettet")
                dummies = pd.get_dummies(df[base], prefix=base, drop_first=True)
                factor_map[v] = list(dummies.columns)

        indep_vars = []
        for v in raw_indep:
            if v in factor_map:
                indep_vars.extend(factor_map[v])
            else:
                indep_vars.append(v)

        cont_vars = [dep_var] + [v for v in indep_vars if v not in
                                  [c for cols in factor_map.values() for c in cols]]
        missing = [v for v in cont_vars if v not in df.columns]
        if missing:
            raise ValueError(f"Variabler ikke funnet: {missing}")
        df_work = df[cont_vars].copy()
        for v in cont_vars:
            df_work[v] = pd.to_numeric(df_work[v], errors='coerce')
        for iv, dummy_cols in factor_map.items():
            base = iv[2:]
            dummies = pd.get_dummies(df[base], prefix=base, drop_first=True)
            for col in dummy_cols:
                df_work[col] = dummies[col].astype(float)

        df_clean = df_work.dropna().copy()
        if df_clean.empty:
            raise ValueError("Ingen observasjoner etter konvertering.")
        for v in cont_vars:
            df_clean[v] = df_clean[v].astype(np.float64)
        for cols in factor_map.values():
            for col in cols:
                df_clean[col] = df_clean[col].astype(np.float64)

        if options.get('standardize'):
            for v in indep_vars:
                std = df_clean[v].std()
                if std > 0:
                    df_clean[v] = (df_clean[v] - df_clean[v].mean()) / std

        Y = df_clean[dep_var]
        X = self._add_const(df_clean[indep_vars], add_const)

        if reg_cmd == 'regress':
            model = sm.OLS(Y, X).fit()
        elif reg_cmd == 'probit':
            model = Probit(Y, X).fit(disp=0)
        elif reg_cmd == 'logit':
            model = sm.Logit(Y, X).fit(disp=0)
        elif reg_cmd == 'poisson':
            model = sm.GLM(Y, X, family=sm.families.Poisson()).fit()
        else:
            raise ValueError(
                f"coefplot støtter ikke '{reg_cmd}'. Bruk: regress, logit, probit, poisson.")
        model = self._apply_cov(model, options, df_clean)
        return model, dep_var, indep_vars, df_clean

    def execute(self, cmd, df, args, options):
        # IV-regresjon har dict-args med dep/exog/endog/instruments
        if cmd in ('ivregress', 'ivregress-predict'):
            return self._execute_iv(cmd, df, args, options)
        if cmd == 'rdd':
            return self._execute_rdd(cmd, df, args, options)

        dep_var = args[0]
        raw_indep = list(args[1:])
        add_const = 'noconstant' not in options
        alpha = 1 - (float(options.get('level', 95)) / 100)

        # ── Stata-stil i.VARNAME: lag dummies for kategoriske variabler ──────
        # i.kjønn → dummy-kolonner kjønn_<kategori2>, kjønn_<kategori3>, …
        # Referansekategori (lavest sortert) droppes automatisk (drop_first=True).
        factor_map = {}  # 'i.xxx' -> [dummy_col1, dummy_col2, ...]
        for v in raw_indep:
            if v.startswith('i.'):
                base = v[2:]
                if base not in df.columns:
                    raise ValueError(f"Faktorvariabel '{base}' finnes ikke i datasettet")
                dummies = pd.get_dummies(df[base], prefix=base, drop_first=True)
                factor_map[v] = list(dummies.columns)

        # Utvidet indep_vars: i.xxx erstattes med sine dummy-kolonnenavn
        indep_vars = []
        for v in raw_indep:
            if v in factor_map:
                indep_vars.extend(factor_map[v])
            else:
                indep_vars.append(v)

        # Kontinuerlige variabler som må konverteres numerisk
        cont_vars = [dep_var] + [v for v in indep_vars if v not in
                                  [col for cols in factor_map.values() for col in cols]]
        if options.get('cluster') and options['cluster'] not in cont_vars:
            cont_vars.append(options['cluster'])

        # Bygg arbeidsdf: numeriske kontinuerlige kolonner
        missing = [v for v in cont_vars if v not in df.columns]
        if missing:
            raise ValueError(f"Variabler ikke funnet i datasettet: {missing}")
        df_work = df[cont_vars].copy()
        for v in cont_vars:
            df_work[v] = pd.to_numeric(df_work[v], errors='coerce')

        # Legg til dummy-kolonner (allerede float/bool fra get_dummies)
        for iv, dummy_cols in factor_map.items():
            base = iv[2:]
            dummies = pd.get_dummies(df[base], prefix=base, drop_first=True)
            for col in dummy_cols:
                df_work[col] = dummies[col].astype(float)

        df_clean = df_work.dropna().copy()
        if df_clean.empty:
            raise ValueError(
                "Ingen observasjoner etter numerisk konvertering — sjekk at avhengig og uavhengige variabler er tall."
            )
        for v in cont_vars:
            df_clean[v] = df_clean[v].astype(np.float64)
        for cols in factor_map.values():
            for col in cols:
                df_clean[col] = df_clean[col].astype(np.float64)

        vars_needed = [dep_var] + indep_vars  # brukes av regress-panel m.fl.
        Y = df_clean[dep_var]
        X = self._add_const(df_clean[indep_vars], add_const)

        if cmd == 'regress':
            model = sm.OLS(Y, X).fit()
            model = self._apply_cov(model, options, df_clean)
            return (str(model.summary(alpha=alpha)), None)

        if cmd == 'probit':
            model = Probit(Y, X).fit(disp=0)
            model = self._apply_cov(model, options, df_clean)
            return (str(model.summary(alpha=alpha)), None)

        if cmd == 'logit':
            model = sm.Logit(Y, X).fit(disp=0)
            model = self._apply_cov(model, options, df_clean)
            if options.get('or'):
                coef = np.exp(model.params)
                conf = np.exp(model.conf_int(alpha=alpha))
                out = f"\nModell: logit (odds ratios)\n{pd.DataFrame({'OR': coef, '2.5%': conf[0], '97.5%': conf[1]})}\n"
                return (out, None)
            return (str(model.summary(alpha=alpha)), None)

        if cmd == 'poisson':
            model = sm.GLM(Y, X, family=sm.families.Poisson()).fit()
            model = self._apply_cov(model, options, df_clean)
            if options.get('irr'):
                coef = np.exp(model.params)
                conf = np.exp(model.conf_int(alpha=alpha))
                out = f"\nModell: poisson (incidence rate ratios)\n{pd.DataFrame({'IRR': coef, '2.5%': conf[0], '97.5%': conf[1]})}\n"
                return (out, None)
            return (str(model.summary(alpha=alpha)), None)

        if cmd in ('regress-panel', 'regress-panel-predict', 'regress-panel-diff'):
            if 'tid' not in df.columns:
                raise ValueError(f"{cmd} krever paneldata (tid-kolonne mangler).")
            _key = _get_df_key_col(df) or 'unit_id'

            # --- regress-panel-diff: bygg interaksjonsledd ---
            if cmd == 'regress-panel-diff':
                if len(args) < 3:
                    raise ValueError("regress-panel-diff krever: depvar group_var treated_var [covariater]")
                group_var = args[1]
                treated_var = args[2]
                extra_covars = list(args[3:])
                interact_col = f'{group_var}_x_{treated_var}'
                df_clean[interact_col] = (df_clean[group_var] * df_clean[treated_var]).astype(float)
                indep_vars = [group_var, treated_var, interact_col] + [v for v in indep_vars if v not in (group_var, treated_var)]
                X = self._add_const(df_clean[indep_vars], add_const)

            panel_df = df_clean.copy()
            panel_df[_key] = df.loc[panel_df.index, _key]
            panel_df['tid'] = df.loc[panel_df.index, 'tid']
            Y_panel = panel_df[dep_var]
            X_panel = self._add_const(panel_df[indep_vars], add_const)
            use_re = options.get('re') or options.get('random')
            use_be = options.get('be')
            use_pooled = options.get('pooled') or cmd == 'regress-panel-diff'

            if use_pooled:
                model = sm.OLS(Y_panel, X_panel).fit()
                model = self._apply_cov(model, options, panel_df)
                if cmd == 'regress-panel-predict':
                    return self._panel_predict_extra(model, Y_panel, X_panel, panel_df, _key, df_clean, options, alpha, 'pooled')
                if cmd == 'regress-panel-diff':
                    atet = model.params.get(interact_col, None)
                    pval = model.pvalues.get(interact_col, None)
                    header = f"\nDiff-in-diff (pooled OLS)\n"
                    header += f"ATET ({interact_col}): {atet:.4f}" + (f", p={pval:.4f}" if pval is not None else "") + "\n\n"
                    return (header + str(model.summary(alpha=alpha)), None)
                return (str(model.summary(alpha=alpha)), None)

            # Prøv linearmodels først
            _use_linearmodels = False
            try:
                from linearmodels.panel import PanelOLS, RandomEffects, BetweenOLS
                _use_linearmodels = True
            except ImportError:
                pass

            if _use_linearmodels:
                panel_idx = panel_df.set_index([_key, 'tid'])
                Y_p = panel_idx[dep_var]
                X_p = self._add_const(panel_idx[indep_vars], add_const)
                if use_re:
                    model = RandomEffects(Y_p, X_p).fit()
                elif use_be:
                    model = BetweenOLS(Y_p, X_p).fit()
                else:
                    model = PanelOLS(Y_p, X_p, entity_effects=True, drop_absorbed=True).fit(
                        cov_type='clustered' if options.get('robust') else 'unadjusted',
                        cluster_entity=options.get('robust', False))
                if cmd == 'regress-panel-predict':
                    extra = {}
                    pred_name = options.get('predicted', 'predicted')
                    res_name = options.get('residuals')
                    eff_name = options.get('effects')
                    fitted = model.fitted_values
                    resids = model.resids
                    if pred_name:
                        extra[str(pred_name) if pred_name is not True else 'predicted'] = pd.Series(
                            fitted.values.ravel(), index=panel_df.index)
                    if res_name:
                        extra[str(res_name) if res_name is not True else 'residuals'] = pd.Series(
                            resids.values.ravel(), index=panel_df.index)
                    if eff_name:
                        try:
                            effs = model.estimated_effects
                            extra[str(eff_name) if eff_name is not True else 'effects'] = pd.Series(
                                effs.values.ravel(), index=panel_df.index)
                        except Exception:
                            pass
                    if not extra:
                        extra['predicted'] = pd.Series(fitted.values.ravel(), index=panel_df.index)
                    return (str(model.summary), extra)
                return (str(model.summary), None)

            # Fallback: kun statsmodels
            g = panel_df[_key]
            if use_re:
                model = sm.MixedLM(Y_panel, X_panel, groups=g).fit(reml=True)
                if cmd == 'regress-panel-predict':
                    return self._panel_predict_extra(model, Y_panel, X_panel, panel_df, _key, df_clean, options, alpha, 're')
                return (str(model.summary(alpha=alpha)), None)
            if use_be:
                between_df = panel_df.groupby(_key)[vars_needed].mean()
                Y_b = between_df[dep_var]
                X_b = self._add_const(between_df[indep_vars], add_const)
                model = sm.OLS(Y_b, X_b).fit()
                return (str(model.summary(alpha=alpha)), None)
            # FE: within-transform (demean by entity)
            Y_w = Y_panel - Y_panel.groupby(g).transform('mean')
            X_w = X_panel.groupby(g).transform(lambda x: x - x.mean())
            if add_const and 'const' in X_w.columns:
                X_w = X_w.drop(columns=['const'], errors='ignore')
            model = sm.OLS(Y_w, X_w).fit()
            if options.get('robust'):
                try:
                    model = model.get_robustcov_results(cov_type='cluster', groups=g.values)
                except Exception:
                    model = model.get_robustcov_results(cov_type='HC1')
            if cmd == 'regress-panel-predict':
                return self._panel_predict_extra(model, Y_w, X_w, panel_df, _key, df_clean, options, alpha, 'fe', g=g, Y_orig=Y_panel, X_orig=X_panel)
            return (str(model.summary(alpha=alpha)), None)

        if cmd == 'hausman':
            if 'tid' not in df.columns:
                raise ValueError("hausman krever paneldata (tid-kolonne mangler).")
            _key = _get_df_key_col(df) or 'unit_id'
            panel_df = df_clean.copy()
            panel_df[_key] = df.loc[panel_df.index, _key]
            panel_df['tid'] = df.loc[panel_df.index, 'tid']
            Y_p = panel_df[dep_var]
            X_p = self._add_const(panel_df[indep_vars], add_const)
            g = panel_df[_key]
            try:
                from linearmodels.panel import PanelOLS, RandomEffects
                panel_idx = panel_df.set_index([_key, 'tid'])
                Y_pi = panel_idx[dep_var]
                X_pi = self._add_const(panel_idx[indep_vars], add_const)
                fe = PanelOLS(Y_pi, X_pi, entity_effects=True, drop_absorbed=True).fit()
                re = RandomEffects(Y_pi, X_pi).fit()
            except ImportError:
                X_w = X_p.groupby(g).transform(lambda x: x - x.mean()).drop(columns=['const'], errors='ignore')
                fe = sm.OLS(Y_p - Y_p.groupby(g).transform('mean'), X_w).fit()
                re = sm.MixedLM(Y_p, X_p, groups=g).fit(reml=True)
                common = fe.params.index.intersection(re.fe_params.index)
                if len(common) == 0:
                    return ("Hausman (statsmodels): kunne ikke aligne FE og RE-koeffisienter.\n", None)
                diff = fe.params.loc[common].values - re.fe_params.loc[common].values
                try:
                    v_fe = fe.cov_params().loc[common, common].values
                    v_re = re.cov_params().loc[common, common].values
                    vdiff = v_fe - v_re
                    from scipy.linalg import inv
                    chi2 = float(diff @ inv(vdiff) @ diff)
                    from scipy.stats import chi2 as chi2_dist
                    pval = 1 - chi2_dist.cdf(chi2, len(diff))
                    out = f"\nHausman (FE vs RE, statsmodels)\nFE (within):\n{fe.summary()}\n\nRE (MixedLM):\n{re.summary()}\n"
                    out += f"\nDifferanse koeff (FE-RE): chi2={chi2:.4f}, P={pval:.4f}\n"
                    out += "P<0.05 => bruk FE. P>=0.05 => bruk RE.\n"
                    return (out, None)
                except Exception as e:
                    return (f"Hausman (statsmodels) feilet: {e}\n", None)
            # linearmodels: bruk .cov for kovariansmatrise (ikke .cov_params())
            common = fe.params.index.intersection(re.params.index)
            if len(common) == 0:
                return (f"\nHausman\nFE:\n{fe.summary}\n\nRE:\n{re.summary}\n\nIngen felles koeffisienter å sammenligne.\n", None)
            diff = fe.params.loc[common] - re.params.loc[common]
            try:
                v_fe = fe.cov.loc[common, common].values if hasattr(fe, 'cov') else fe.variance_decomposition
                v_re = re.cov.loc[common, common].values if hasattr(re, 'cov') else np.zeros_like(v_fe)
            except Exception:
                v_fe = np.diag(fe.std_errors.loc[common].values ** 2)
                v_re = np.diag(re.std_errors.loc[common].values ** 2)
            vdiff = v_fe - v_re
            try:
                chi2 = float(diff.values @ np.linalg.solve(vdiff, diff.values))
                from scipy.stats import chi2 as chi2_dist
                pval = 1 - chi2_dist.cdf(chi2, len(diff))
                out = f"\nHausman (FE vs RE)\nFE:\n{fe.summary}\n\nRE:\n{re.summary}\n"
                out += f"\nDifferanse koeff: {diff.to_dict()}\nchi2={chi2:.4f}, P={pval:.4f}\n"
                out += "P<0.05 => bruk FE. P>=0.05 => bruk RE.\n"
                return (out, None)
            except Exception as e:
                return (f"Hausman feilet: {e}\n", None)

        if cmd == 'regress-predict':
            model = sm.OLS(Y, X).fit()
            pred_name = options.get('predicted', 'predicted')
            res_name = options.get('residuals')
            cook_name = options.get('cooksd')
            extra = {}
            if pred_name:
                extra[str(pred_name) if pred_name != True else 'predicted'] = pd.Series(model.predict(), index=df_clean.index)
            if res_name:
                extra[str(res_name) if res_name != True else 'residuals'] = pd.Series(model.resid, index=df_clean.index)
            if cook_name:
                from statsmodels.stats.outliers_influence import OLSInfluence
                inf = OLSInfluence(model)
                extra[str(cook_name) if cook_name != True else 'cooksd'] = pd.Series(inf.cooks_distance[0], index=df_clean.index)
            return (str(model.summary(alpha=alpha)), extra)

        if cmd in ('probit-predict', 'logit-predict'):
            if cmd == 'probit-predict':
                model = Probit(Y, X).fit(disp=0)
            else:
                model = sm.Logit(Y, X).fit(disp=0)
            model = self._apply_cov(model, options, df_clean)
            extra = {}
            pred_name = options.get('predicted')
            prob_name = options.get('probabilities')
            res_name = options.get('residuals')
            # Predikerte sannsynligheter (P(Y=1|X))
            probs = pd.Series(model.predict(), index=df_clean.index)
            if prob_name:
                extra[str(prob_name) if prob_name is not True else 'probabilities'] = probs
            # Lineær prediksjon (Xβ)
            if pred_name:
                xb = pd.Series(X @ model.params, index=df_clean.index)
                extra[str(pred_name) if pred_name is not True else 'predicted'] = xb
            # Residualer (Y - P(Y=1|X))
            if res_name:
                extra[str(res_name) if res_name is not True else 'residuals'] = pd.Series(model.resid_response, index=df_clean.index)
            # Hvis ingen options spesifisert, gi sannsynligheter som default
            if not extra:
                extra['predicted_prob'] = probs
            return (str(model.summary(alpha=alpha)), extra)

        if cmd in ('mlogit', 'mlogit-predict'):
            from statsmodels.discrete.discrete_model import MNLogit
            # Y skal være kategorisk — bruk originale verdier (ikke float-konvertert)
            Y_cat = df_clean[dep_var]
            # Sorter kategorier for konsistent rekkefølge
            cats = sorted(Y_cat.unique())
            model = MNLogit(Y_cat, X).fit(disp=0)
            model = self._apply_cov(model, options, df_clean)

            extra = None
            if cmd == 'mlogit-predict':
                extra = {}
                pred_name = options.get('predicted')
                prob_name = options.get('probabilities')
                res_name = options.get('residuals')
                # Predikerte sannsynligheter per kategori: prob6_1, prob6_2, ...
                pred_probs = model.predict()  # n × K matrise
                cat_labels = [str(int(c)) if float(c) == int(c) else str(c) for c in cats]
                if prob_name:
                    base = str(prob_name) if prob_name is not True else 'prob'
                    for i, cat in enumerate(cat_labels):
                        extra[f'{base}_{cat}'] = pd.Series(pred_probs[:, i], index=df_clean.index)
                # Lineær prediksjon (Xβ) per kategori (relativt til referansekategori)
                if pred_name:
                    base = str(pred_name) if pred_name is not True else 'predicted'
                    for i, cat in enumerate(cat_labels):
                        if i == 0:
                            # Referansekategori: Xβ = 0 per definisjon
                            extra[f'{base}_{cat}'] = pd.Series(0.0, index=df_clean.index)
                        else:
                            xb = pd.Series(X @ model.params.iloc[:, i - 1], index=df_clean.index)
                            extra[f'{base}_{cat}'] = xb
                if res_name:
                    base = str(res_name) if res_name is not True else 'residuals'
                    for i, cat in enumerate(cat_labels):
                        y_i = (Y_cat == cats[i]).astype(float)
                        extra[f'{base}_{cat}'] = pd.Series(y_i.values - pred_probs[:, i], index=df_clean.index)
                if not extra:
                    base = 'prob'
                    for i, cat in enumerate(cat_labels):
                        extra[f'{base}_{cat}'] = pd.Series(pred_probs[:, i], index=df_clean.index)
            return (str(model.summary(alpha=alpha)), extra)

        return (f"Ukjent regresjonskommando: {cmd}", None)

    def _execute_iv(self, cmd, df, args, options):
        alpha = 1 - (float(options.get('level', 95)) / 100)
        dep = args.get('dep')
        exog_vars = args.get('exog', [])
        endog_vars = args.get('endog', [])
        instr_vars = args.get('instruments', [])
        if not dep or not endog_vars or not instr_vars:
            raise ValueError(
                "ivregress krever: depvar [exog...] (endog = instrumenter). "
                "Eksempel: ivregress lønn mann (formuehøy = alder)"
            )
        all_vars = [dep] + exog_vars + endog_vars + instr_vars
        missing = [v for v in all_vars if v not in df.columns]
        if missing:
            raise ValueError(f"Variabler mangler i datasettet: {missing}")
        df_iv = df[all_vars].dropna()
        for v in all_vars:
            df_iv[v] = pd.to_numeric(df_iv[v], errors='coerce')
        df_iv = df_iv.dropna()
        if df_iv.empty:
            raise ValueError("Ingen observasjoner etter fjerning av manglende verdier.")
        Y = df_iv[dep].astype(float)

        # Stage 1: project endogenous vars onto instruments + exog
        Z = sm.add_constant(df_iv[instr_vars + exog_vars].astype(float))
        endog_fitted = pd.DataFrame(index=df_iv.index)
        first_stage_lines = []
        for ev in endog_vars:
            fs = sm.OLS(df_iv[ev].astype(float), Z).fit()
            endog_fitted[ev] = fs.predict()
            f_stat = fs.fvalue
            first_stage_lines.append(f"  Første trinn ({ev}): F={f_stat:.2f}, R²={fs.rsquared:.4f}")

        # Stage 2: regress Y on [exog + fitted endog]
        X2 = df_iv[exog_vars].astype(float).copy() if exog_vars else pd.DataFrame(index=df_iv.index)
        for ev in endog_vars:
            X2[ev] = endog_fitted[ev]
        X2 = sm.add_constant(X2)
        model_2s = sm.OLS(Y, X2).fit()

        # 2SLS residuals use actual endog values, not fitted
        X_actual = df_iv[exog_vars + endog_vars].astype(float).copy() if (exog_vars or endog_vars) else pd.DataFrame(index=df_iv.index)
        X_actual = sm.add_constant(X_actual)
        X_actual = X_actual.reindex(columns=X2.columns, fill_value=0.0)
        predicted_vals = X_actual @ model_2s.params
        resid_vals = Y - predicted_vals

        method = args.get('method', '2sls').upper()
        header = f"\nInstrumentvariabelregresjon ({method})\n"
        header += "\n".join(first_stage_lines) + "\n\n"
        header += f"Andre trinn (avhengig: {dep}):\n"
        summary = header + str(model_2s.summary(alpha=alpha))

        extra = None
        if cmd == 'ivregress-predict':
            extra = {}
            pred_name = options.get('predicted')
            res_name = options.get('residuals')
            if pred_name:
                extra[str(pred_name) if pred_name is not True else 'predicted'] = pd.Series(predicted_vals, index=df_iv.index)
            elif not res_name:
                extra['predicted'] = pd.Series(predicted_vals, index=df_iv.index)
            if res_name:
                extra[str(res_name) if res_name is not True else 'residuals'] = pd.Series(resid_vals, index=df_iv.index)
        return (summary, extra)

    def _execute_rdd(self, cmd, df, args, options):
        """Regression Discontinuity Design (sharp og fuzzy)."""
        dep = args.get('dep')
        runvar = args.get('runvar')
        raw_exog = args.get('exog', [])
        cutoff = float(options.get('cutoff', 0))
        poly_order = int(options.get('polynomial', 1))
        fuzzy_var = options.get('fuzzy')
        deriv = int(options.get('derivate', 0))
        alpha = 1 - (float(options.get('level', 95)) / 100)
        cluster_var = options.get('cluster')

        if not dep or not runvar:
            raise ValueError("rdd krever: depvar runvar [covariater]. Eksempel: rdd vote margin")

        # i. prefix → dummies
        exog_cols = []
        for v in raw_exog:
            if v.startswith('i.'):
                base = v[2:]
                if base in df.columns:
                    dummies = pd.get_dummies(df[base], prefix=base, drop_first=True).astype(float)
                    for col in dummies.columns:
                        df[col] = dummies[col]
                    exog_cols.extend(dummies.columns)
            elif v in df.columns:
                exog_cols.append(v)

        all_vars = [dep, runvar] + list(exog_cols)
        if fuzzy_var:
            if fuzzy_var not in df.columns:
                raise ValueError(f"Fuzzy-variabel '{fuzzy_var}' finnes ikke i datasettet.")
            all_vars.append(fuzzy_var)
        if cluster_var and cluster_var in df.columns:
            all_vars.append(cluster_var)

        missing = [v for v in all_vars if v not in df.columns]
        if missing:
            raise ValueError(f"Variabler mangler i datasettet: {missing}")

        df_rdd = df[all_vars].copy()
        for v in [dep, runvar] + list(exog_cols):
            df_rdd[v] = pd.to_numeric(df_rdd[v], errors='coerce')
        if fuzzy_var:
            df_rdd[fuzzy_var] = pd.to_numeric(df_rdd[fuzzy_var], errors='coerce')
        df_rdd = df_rdd.dropna()
        if df_rdd.empty:
            raise ValueError("Ingen observasjoner etter fjerning av manglende verdier.")
        # Sikre float64 for alle numeriske kolonner (unngå object-dtype i numpy)
        for v in [dep, runvar] + list(exog_cols):
            df_rdd[v] = df_rdd[v].astype(np.float64)

        Y = df_rdd[dep].values
        X_run = df_rdd[runvar].values
        covs = df_rdd[exog_cols].values.astype(np.float64) if exog_cols else None
        fuzzy_T = df_rdd[fuzzy_var].values if fuzzy_var else None
        if cluster_var and cluster_var in df_rdd.columns:
            cl_col = df_rdd[cluster_var]
            if not pd.api.types.is_numeric_dtype(cl_col):
                cluster_vals = cl_col.astype('category').cat.codes.values.astype(np.int64)
            else:
                cluster_vals = cl_col.values.astype(np.int64)
        else:
            cluster_vals = None

        # Prøv rdrobust-pakken
        try:
            from rdrobust import rdrobust as _rdrobust
            rdd_kwargs = dict(y=Y, x=X_run, c=cutoff, p=poly_order, deriv=deriv)
            if covs is not None and covs.shape[1] > 0:
                rdd_kwargs['covs'] = covs
            if fuzzy_T is not None:
                rdd_kwargs['fuzzy'] = fuzzy_T
            if cluster_vals is not None:
                rdd_kwargs['cluster'] = cluster_vals
            result = _rdrobust(**rdd_kwargs)

            # rdrobust returnerer DataFrames — hent verdier via .iloc
            coef_df = result.coef
            se_df = result.se
            pv_df = result.pv
            ci_df = result.ci
            bws_df = result.bws
            N_list = result.N

            summary_df = pd.DataFrame({
                'Estimat': coef_df.iloc[:, 0].values,
                'Std.feil': se_df.iloc[:, 0].values,
                'p-verdi': pv_df.iloc[:, 0].values,
                'KI nedre': ci_df.iloc[:, 0].values,
                'KI øvre': ci_df.iloc[:, 1].values,
            }, index=coef_df.index)

            info_lines = [f"Running variable: {runvar}"]
            info_lines.append(f"Cutoff: {cutoff}")
            info_lines.append(f"Polynomial-orden: {poly_order}")
            try:
                info_lines.append(f"Båndbredde (h): venstre={bws_df.iloc[0, 0]:.2f}, høyre={bws_df.iloc[0, 1]:.2f}")
            except Exception:
                pass
            if N_list:
                info_lines.append(f"N: venstre={int(N_list[0])}, høyre={int(N_list[1])}")
            if fuzzy_var:
                info_lines.append(f"Fuzzy: {fuzzy_var}")
            info_text = "\n".join(info_lines)

            return (f"\nRDD (Regression Discontinuity Design)\n{info_text}\n\n{summary_df.to_string()}\n", None)

        except ImportError:
            pass
        except Exception:
            # rdrobust feilet — fall tilbake til manuell OLS
            pass

        # Fallback: manuell lokal lineær regresjon med statsmodels
        R = X_run - cutoff
        T = (R >= 0).astype(float)
        T_R = T * R
        X_cols = {'const': 1.0, 'T': T, 'R': R, 'T_R': T_R}
        if poly_order >= 2:
            R2 = R ** 2
            T_R2 = T * R2
            X_cols['R2'] = R2
            X_cols['T_R2'] = T_R2
        X_df = pd.DataFrame(X_cols, index=df_rdd.index)
        if covs is not None and covs.shape[1] > 0:
            for ci_idx, col in enumerate(exog_cols):
                X_df[col] = covs[:, ci_idx]

        if fuzzy_var:
            # Fuzzy RDD: 2SLS med T som instrument for fuzzy_var
            Z = X_df.copy()
            fs = sm.OLS(fuzzy_T, Z).fit()
            fuzzy_hat = fs.predict()
            X_2s = X_df.drop(columns=['T']).copy()
            X_2s[fuzzy_var] = fuzzy_hat
            model = sm.OLS(Y, X_2s).fit()
            disc_param = fuzzy_var
        else:
            model = sm.OLS(Y, X_df).fit()
            disc_param = 'T'

        # Cluster / robust
        if cluster_var and cluster_vals is not None:
            try:
                model = model.get_robustcov_results(cov_type='cluster', groups=cluster_vals)
            except Exception:
                pass
        elif options.get('robust'):
            try:
                model = model.get_robustcov_results(cov_type='HC1')
            except Exception:
                pass

        disc = model.params[disc_param]
        disc_se = model.bse[disc_param]
        disc_p = model.pvalues[disc_param]
        from scipy.stats import norm
        z_crit = norm.ppf(1 - alpha / 2)
        ci_lo = disc - z_crit * disc_se
        ci_hi = disc + z_crit * disc_se

        n_left = int((R < 0).sum())
        n_right = int((R >= 0).sum())

        info = (
            f"\nRDD (Regression Discontinuity Design)\n"
            f"Running variable: {runvar}\n"
            f"Cutoff: {cutoff}\n"
            f"Polynomial-orden: {poly_order}\n"
            f"N: venstre={n_left}, høyre={n_right}\n"
        )
        if fuzzy_var:
            info += f"Fuzzy: {fuzzy_var}\n"
            info += f"Første trinn F-stat: {fs.fvalue:.2f}\n"

        rows = [{
            'Estimat': disc,
            'Std.feil': disc_se,
            'z': disc / disc_se if disc_se > 0 else np.nan,
            'p-verdi': disc_p,
            f'KI nedre {int((1-alpha)*100)}%': ci_lo,
            f'KI øvre {int((1-alpha)*100)}%': ci_hi,
        }]
        summary_df = pd.DataFrame(rows, index=['Diskontinuitet'])
        return (f"{info}\n{summary_df.to_string()}\n", None)


class SurvivalHandler:
    """Overlevelsesanalyse med lifelines: cox, kaplan-meier, weibull."""

    def __init__(self):
        self.default_decimals = 2

    def execute(self, cmd, df, args, options):
        try:
            import lifelines
        except ImportError:
            raise ImportError("lifelines må være installert for overlevelsesanalyse. Kjør: pip install lifelines")

        if cmd == 'cox':
            return self._cox(df, args, options)
        if cmd == 'kaplan-meier':
            return self._kaplan_meier(df, args, options)
        if cmd == 'weibull':
            return self._weibull(df, args, options)
        return (f"Ukjent overlevelseskommando: {cmd}", None)

    def _cox(self, df, args, options):
        from lifelines import CoxPHFitter

        if not isinstance(args, (list, tuple)) or len(args) < 2:
            return (f"cox krever hendelse-var og tid-var.", None)
        event_var, duration_var = args[0], args[1]
        raw_covars = list(args[2:])
        # i.VARNAME → dummies (Stata-stil)
        factor_dummies = {}
        covars = []
        for v in raw_covars:
            if v.startswith('i.'):
                base = v[2:]
                if base in df.columns:
                    dummies = pd.get_dummies(df[base], prefix=base, drop_first=True).astype(float)
                    for col in dummies.columns:
                        df[col] = dummies[col]
                    factor_dummies[v] = list(dummies.columns)
                    covars.extend(dummies.columns)
            elif v in df.columns:
                covars.append(v)
        if event_var not in df.columns or duration_var not in df.columns:
            return (f"cox: variabler {event_var} eller {duration_var} finnes ikke.", None)
        cols = [event_var, duration_var] + list(covars)
        sub = df[cols].dropna(how='any')
        sub = sub[sub[duration_var] > 0]
        if sub.empty or len(sub) < 3:
            return ("cox: for få observasjoner etter dropna (varighet må være > 0).", None)
        level = float(options.get('level', 95)) / 100
        alpha = 1 - level
        cph = CoxPHFitter(alpha=alpha)
        cph.fit(sub, duration_col=duration_var, event_col=event_var)
        if options.get('hazard'):
            hr = cph.hazard_ratios_
            return (hr.to_frame('Hazard Ratio'), None)
        if hasattr(cph, 'summary'):
            return (cph.summary.T, None)
        return (str(cph.print_summary()), None)

    def _kaplan_meier(self, df, args, options):
        from lifelines import KaplanMeierFitter
        import plotly.graph_objects as go

        if not isinstance(args, (list, tuple)) or len(args) < 2:
            return ("kaplan-meier krever hendelse-var og tid-var.", None)
        event_var, duration_var = args[0], args[1]
        if event_var not in df.columns or duration_var not in df.columns:
            return (f"kaplan-meier: variabler {event_var} eller {duration_var} finnes ikke.", None)
        by_var = options.get('by')
        alpha = 1 - float(options.get('level', 95)) / 100
        km_rows = []
        if by_var and by_var in df.columns:
            groups = df.groupby(by_var, dropna=False)
            fig = go.Figure()
            lm = options.get('_label_manager')
            for name, grp in groups:
                sub = grp[[event_var, duration_var]].dropna(how='any')
                if sub.empty:
                    continue
                lbl = lm.format_value(by_var, name) if lm else str(name)
                kmf = KaplanMeierFitter(alpha=alpha)
                kmf.fit(sub[duration_var], sub[event_var], label=lbl)
                sf = kmf.survival_function_
                ci = kmf.confidence_interval_
                fig.add_trace(go.Scatter(x=sf.index, y=sf.iloc[:, 0], mode='lines', name=str(lbl)))
                if ci is not None and not ci.empty:
                    fig.add_trace(go.Scatter(x=ci.index, y=ci.iloc[:, 0], mode='lines', line=dict(dash='dash'), showlegend=False))
                    fig.add_trace(go.Scatter(x=ci.index, y=ci.iloc[:, 1], mode='lines', line=dict(dash='dash'), showlegend=False))
                median = kmf.median_survival_time_
                km_rows.append({
                    'Gruppe': lbl,
                    'N': len(sub),
                    'Hendelser': int(sub[event_var].sum()),
                    'Median overlevelsestid': _smart_float_fmt(median, self.default_decimals) if np.isfinite(median) else '-',
                })
        else:
            sub = df[[event_var, duration_var]].dropna(how='any')
            if sub.empty:
                return ("kaplan-meier: for få observasjoner.", None)
            kmf = KaplanMeierFitter(alpha=alpha)
            kmf.fit(sub[duration_var], sub[event_var])
            sf = kmf.survival_function_
            ci = kmf.confidence_interval_
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=sf.index, y=sf.iloc[:, 0], mode='lines', name='S(t)'))
            if ci is not None and not ci.empty:
                fig.add_trace(go.Scatter(x=ci.index, y=ci.iloc[:, 0], mode='lines', line=dict(dash='dash')))
                fig.add_trace(go.Scatter(x=ci.index, y=ci.iloc[:, 1], mode='lines', line=dict(dash='dash')))
            median = kmf.median_survival_time_
            km_rows.append({
                'N': len(sub),
                'Hendelser': int(sub[event_var].sum()),
                'Median overlevelsestid': _smart_float_fmt(median, self.default_decimals) if np.isfinite(median) else '-',
            })
        fig.update_layout(template='plotly_white', xaxis_title='Tid', yaxis_title='Overlevelsesrate S(t)', margin=dict(l=50, r=50, t=40, b=60))
        summary_df = pd.DataFrame(km_rows)
        if 'Gruppe' in summary_df.columns:
            summary_df = summary_df.set_index('Gruppe')
        return (summary_df, fig)

    def _weibull(self, df, args, options):
        from lifelines import WeibullAFTFitter
        import plotly.graph_objects as go

        if not isinstance(args, (list, tuple)) or len(args) < 2:
            return ("weibull krever hendelse-var og tid-var.", None)
        event_var, duration_var = args[0], args[1]
        if event_var not in df.columns or duration_var not in df.columns:
            return (f"weibull: variabler {event_var} eller {duration_var} finnes ikke.", None)
        by_var = options.get('by')
        alpha = 1 - float(options.get('level', 95)) / 100
        wb_rows = []
        summaries = []
        if by_var and by_var in df.columns:
            groups = df.groupby(by_var, dropna=False)
            fig = go.Figure()
            lm = options.get('_label_manager')
            for name, grp in groups:
                sub = grp[[event_var, duration_var]].dropna(how='any')
                sub = sub[sub[duration_var] > 0]
                if sub.empty or len(sub) < 5:
                    continue
                lbl = lm.format_value(by_var, name) if lm else str(name)
                try:
                    waf = WeibullAFTFitter(alpha=alpha)
                    waf.fit(sub, duration_col=duration_var, event_col=event_var)
                    times = np.linspace(sub[duration_var].min(), sub[duration_var].max(), 100)
                    pred = waf.predict_survival_function(sub, times=times)
                    if hasattr(pred, 'mean'):
                        s = pred.mean(axis=1)
                    else:
                        s = pred.iloc[:, 0] if hasattr(pred, 'iloc') else pred
                    fig.add_trace(go.Scatter(x=times, y=s.values if hasattr(s, 'values') else s, mode='lines', name=str(lbl)))
                    # Hent nøkkelparametre
                    row = {'Gruppe': lbl, 'N': len(sub), 'Hendelser': int(sub[event_var].sum())}
                    if hasattr(waf, 'lambda_') and hasattr(waf, 'rho_'):
                        row['lambda'] = float(waf.lambda_)
                        row['rho'] = float(waf.rho_)
                    elif hasattr(waf, 'summary'):
                        params = waf.summary['coef']
                        for pname, pval in params.items():
                            # pname kan være tuple ('lambda_', 'Intercept') — forenkle
                            key = pname[0] if isinstance(pname, tuple) else str(pname)
                            row[key] = round(float(pval), 4)
                    wb_rows.append(row)
                except Exception as e:
                    wb_rows.append({'Gruppe': lbl, 'N': len(sub), 'Feil': str(e)})
        else:
            sub = df[[event_var, duration_var]].dropna(how='any')
            sub = sub[sub[duration_var] > 0]
            if sub.empty or len(sub) < 5:
                return ("weibull: for få observasjoner (varighet må være > 0).", None)
            try:
                waf = WeibullAFTFitter(alpha=alpha)
                waf.fit(sub, duration_col=duration_var, event_col=event_var)
                times = np.linspace(sub[duration_var].min(), sub[duration_var].max(), 100)
                pred = waf.predict_survival_function(sub, times=times)
                s = pred.mean(axis=1) if hasattr(pred, 'mean') else pred.iloc[:, 0]
                fig = go.Figure(data=[go.Scatter(x=times, y=s.values, mode='lines', name='S(t)')])
                if hasattr(waf, 'summary'):
                    summaries = [waf.summary.T]
                else:
                    summaries = []
            except Exception as e:
                return (f"weibull feilet: {e}", None)
        fig.update_layout(template='plotly_white', xaxis_title='Tid', yaxis_title='Overlevelsesrate S(t)', margin=dict(l=50, r=50, t=40, b=60))
        if wb_rows:
            summary_df = pd.DataFrame(wb_rows)
            if 'Gruppe' in summary_df.columns:
                summary_df = summary_df.set_index('Gruppe')
        elif summaries:
            summary_df = summaries[0]
        else:
            summary_df = pd.DataFrame()
        return (summary_df, fig)


# Markører for embeddable objekter i output (figure, image, etc.)
MICRO_EMBED_START = "__micro_transform_start_{}__"
MICRO_EMBED_END = "__micro_transform_end__"


# Standardfarger for Plotly: grønn for histogram, grønn-basert palett for bar/serie med flere kategorier
PLOTLY_DEFAULT_GREEN = '#2e7d32'
PLOTLY_BAR_PALETTE = [
    '#2e7d32',  # grønn
    '#1a5f7a',  # blå/teal
    '#1565c0',  # blå
    '#6a1b9a',  # lilla
    '#e65100',  # oransje
    '#c62828',  # rød
    '#00838f',  # cyan
    '#7b1fa2',  # lilla
]


class PlotHandler:
    """Genererer Plotly-figurer for barchart, histogram, boxplot, scatter, piechart, hexbin, sankey."""

    @staticmethod
    def _bar_colors(n, has_labels_or_multiple):
        """Returnerer én farge (grønn) eller liste med grønn som start for bar/histogram."""
        if n <= 0:
            return PLOTLY_DEFAULT_GREEN
        if n == 1 or not has_labels_or_multiple:
            return PLOTLY_DEFAULT_GREEN
        return [PLOTLY_BAR_PALETTE[i % len(PLOTLY_BAR_PALETTE)] for i in range(n)]

    def execute(self, cmd, df, args, options):
        try:
            import plotly.graph_objects as go
        except ImportError:
            raise ImportError("plotly må være installert for figurkommandoer. Kjør: pip install plotly")

        if cmd == 'barchart':
            return self._barchart(df, args, options)
        if cmd == 'histogram':
            return self._histogram(df, args, options)
        if cmd == 'boxplot':
            return self._boxplot(df, args, options)
        if cmd == 'scatter':
            return self._scatter(df, args, options)
        if cmd == 'piechart':
            return self._piechart(df, args, options)
        if cmd == 'hexbin':
            return self._hexbin(df, args, options)
        if cmd == 'sankey':
            return self._sankey(df, args, options)
        return None

    def _format_labels(self, options, var_name, values):
        """Bruk label_manager for å mappe verdier til labels."""
        lm = options.get('_label_manager')
        if not lm:
            return values
        return [lm.format_value(var_name, v) for v in values]

    def _barchart(self, df, args, options):
        import plotly.graph_objects as go

        if 'raw' in args or not args.get('vars'):
            return None
        stat = args.get('stat', 'count').lower()
        vars_list = [v for v in args['vars'] if v in df.columns]
        if not vars_list:
            return None
        over_var = options.get('over')
        horizontal = options.get('horizontal', False)

        # count/percent: kategorisk variabel
        lm = options.get('_label_manager')
        agg_map = {'mean': 'mean', 'median': 'median', 'sum': 'sum', 'sd': 'std', 'min': 'min', 'max': 'max'}
        if stat in ('count', 'percent'):
            if len(vars_list) > 1:
                # Én søyle per variabel: antall ikke-missing (Stata graph bar-semantikk).
                # For kategorisk fordeling over én variabel, bruk single-var eller over().
                counts = [int(df[v].count()) for v in vars_list]
                if stat == 'percent':
                    total = sum(counts) or 1
                    y_vals = [round(c / total * 100, 1) for c in counts]
                else:
                    y_vals = counts
                x_vals = vars_list
                colors = self._bar_colors(len(x_vals), True)
                fig = go.Figure(data=[go.Bar(
                    x=x_vals if not horizontal else y_vals,
                    y=y_vals if not horizontal else x_vals,
                    orientation='h' if horizontal else 'v',
                    marker_color=colors)])
            else:
                var = vars_list[0]
                if over_var and over_var in df.columns:
                    # Stacked/grouped: én trace per kategori i var, gruppert på over_var
                    ct = pd.crosstab(df[over_var], df[var], dropna=False)
                    if stat == 'percent':
                        ct = ct.div(ct.sum(axis=1), axis=0).multiply(100).round(1)
                    over_labels = self._format_labels(options, over_var, ct.index.tolist())
                    fig = go.Figure()
                    for col in ct.columns:
                        col_label = self._format_labels(options, var, [col])[0] if lm else str(col)
                        fig.add_trace(go.Bar(name=str(col_label), x=over_labels, y=ct[col].values))
                    fig.update_layout(barmode='stack' if 'stack' in options else 'group')
                else:
                    s = df[var].value_counts(dropna=False).sort_index()
                    if stat == 'percent':
                        s = (s / s.sum() * 100).round(1)
                    labels = self._format_labels(options, var, s.index.tolist())
                    x_vals, y_vals = (labels, s.values) if not horizontal else (s.values, labels)
                    n_bars = len(x_vals)
                    has_labels = lm and lm.get_codelist_for_var(var)
                    colors = self._bar_colors(n_bars, has_labels and n_bars > 1)
                    fig = go.Figure(data=[go.Bar(
                        x=x_vals, y=y_vals, orientation='h' if horizontal else 'v',
                        marker_color=colors
                    )])
        else:
            # mean, median, sum, sd, min, max: numerisk med optional over()
            agg_fn = agg_map.get(stat, 'mean')
            if len(vars_list) > 1:
                # Flere numeriske variabler: én søyle per variabel (evt. per gruppe)
                if over_var and over_var in df.columns:
                    fig = go.Figure()
                    for var in vars_list:
                        grp = df.groupby(over_var, dropna=False)[var].agg(agg_fn)
                        x_vals = self._format_labels(options, over_var, grp.index.tolist())
                        fig.add_trace(go.Bar(name=var, x=x_vals, y=grp.values))
                    fig.update_layout(barmode='stack' if 'stack' in options else 'group')
                else:
                    x_vals = vars_list
                    y_vals = [df[v].agg(agg_fn) for v in vars_list]
                    colors = self._bar_colors(len(x_vals), True)
                    fig = go.Figure(data=[go.Bar(x=x_vals, y=y_vals,
                                                  orientation='h' if horizontal else 'v',
                                                  marker_color=colors)])
            else:
                var = vars_list[0]
                if over_var and over_var in df.columns:
                    grp = df.groupby(over_var, dropna=False)[var].agg(agg_fn)
                    x_vals = self._format_labels(options, over_var, grp.index.tolist())
                    y_vals = grp.values
                    n_bars = len(x_vals)
                    has_labels = lm and lm.get_codelist_for_var(over_var)
                    colors = self._bar_colors(n_bars, has_labels and n_bars > 1)
                else:
                    x_vals = [var]
                    y_vals = [df[var].agg(agg_fn)]
                    colors = PLOTLY_DEFAULT_GREEN
                fig = go.Figure(data=[go.Bar(
                    x=x_vals, y=y_vals, orientation='h' if horizontal else 'v',
                    marker_color=colors
                )])

        _var_label = vars_list[0] if len(vars_list) == 1 else ''
        _x_title = stat if stat in ('count', 'percent') else _var_label
        _y_title = '' if horizontal else (stat if stat in ('count', 'percent') else _var_label)
        fig.update_layout(
            template='plotly_white',
            margin=dict(l=50, r=50, t=40, b=60),
            xaxis_title=_x_title,
            yaxis_title=_y_title,
        )
        return fig

    def _histogram(self, df, args, options):
        import plotly.graph_objects as go

        if 'raw' in args or not args.get('vars'):
            return None
        var = args['vars'][0]
        if var not in df.columns:
            return None
        discrete = options.get('discrete', False)
        nbins = options.get('bin') or options.get('nbins')
        try:
            nbins = int(nbins) if nbins else 30
        except (ValueError, TypeError):
            nbins = 30
        density = bool(options.get('density'))
        percent = bool(options.get('percent'))
        freq = bool(options.get('freq'))
        show_normal = bool(options.get('normal'))

        s = df[var].dropna()
        if s.empty:
            # Tom data — returner en tom figur i stedet for feilmelding
            fig = go.Figure()
            fig.update_layout(template='plotly_white', margin=dict(l=50, r=50, t=40, b=60),
                              xaxis_title=var, yaxis_title='Antall',
                              annotations=[dict(text='Ingen data', xref='paper', yref='paper',
                                                x=0.5, y=0.5, showarrow=False)])
            return fig
        if discrete or not pd.api.types.is_numeric_dtype(s):
            vc = s.value_counts().sort_index()
            if percent:
                vc = (vc / vc.sum() * 100).round(2)
            n_bars = len(vc)
            lm = options.get('_label_manager')
            has_labels = lm and lm.get_codelist_for_var(var)
            colors = self._bar_colors(n_bars, has_labels)
            fig = go.Figure(data=[go.Bar(x=vc.index.tolist(), y=vc.values, marker_color=colors)])
            y_title = 'Prosent' if percent else 'Antall'
        else:
            if density:
                histnorm = 'probability density'
                y_title = 'Tetthet'
            elif percent:
                histnorm = 'percent'
                y_title = 'Prosent'
            else:
                histnorm = ''
                y_title = 'Antall'
            fig = go.Figure(data=[go.Histogram(
                x=s, nbinsx=nbins,
                marker_color=PLOTLY_DEFAULT_GREEN,
                histnorm=histnorm or None
            )])
            if show_normal:
                # Overlegg normalfordeling
                import numpy as _np
                mu, sigma = float(s.mean()), float(s.std())
                x_range = _np.linspace(float(s.min()), float(s.max()), 200)
                from scipy.stats import norm as _norm
                y_pdf = _norm.pdf(x_range, mu, sigma)
                if density:
                    y_curve = y_pdf
                elif percent:
                    bin_width = (float(s.max()) - float(s.min())) / nbins
                    y_curve = y_pdf * bin_width * 100
                else:
                    bin_width = (float(s.max()) - float(s.min())) / nbins
                    y_curve = y_pdf * bin_width * len(s)
                fig.add_trace(go.Scatter(
                    x=x_range.tolist(), y=y_curve.tolist(),
                    mode='lines', line=dict(color='red', width=2),
                    name=f'Normal(μ={mu:.1f}, σ={sigma:.1f})'
                ))
        fig.update_layout(template='plotly_white', margin=dict(l=50, r=50, t=40, b=60),
                          xaxis_title=var, yaxis_title=y_title)
        return fig

    def _boxplot(self, df, args, options):
        import plotly.express as px
        import plotly.graph_objects as go

        if 'raw' in args or not args.get('vars'):
            return None
        vars_ = [v for v in args['vars'] if v in df.columns]
        if not vars_:
            return None
        over_var = options.get('over')
        lm = options.get('_label_manager')

        if len(vars_) > 1:
            # Multiple variables: one box trace per variable, ignore over
            fig = go.Figure()
            for var in vars_:
                s = df[var].dropna()
                if not s.empty:
                    fig.add_trace(go.Box(y=s, name=var))
            fig.update_layout(template='plotly_white', margin=dict(l=50, r=50, t=40, b=60),
                              xaxis_title='', yaxis_title='')
        else:
            var = vars_[0]
            s = df[var].dropna()
            if s.empty:
                return None
            if over_var and over_var in df.columns:
                if lm and lm.get_codelist_for_var(over_var):
                    fig = go.Figure()
                    for val in sorted(df[over_var].dropna().unique()):
                        label = lm.format_value(over_var, val)
                        subset = df.loc[df[over_var] == val, var].dropna()
                        if not subset.empty:
                            fig.add_trace(go.Box(y=subset, name=str(label)))
                else:
                    fig = px.box(df, x=over_var, y=var)
            else:
                fig = px.box(df, y=var)
            fig.update_layout(template='plotly_white', margin=dict(l=50, r=50, t=40, b=60),
                              xaxis_title=over_var or '', yaxis_title=var)
        return fig

    def _scatter(self, df, args, options):
        import plotly.graph_objects as go
        import numpy as _np

        if 'raw' in args or len(args.get('vars', [])) < 2:
            return None
        var_x, var_y = args['vars'][0], args['vars'][1]
        if var_x not in df.columns or var_y not in df.columns:
            return None
        by_var = options.get('by') or options.get('color')
        lm = options.get('_label_manager')
        show_lfit = bool(options.get('lfit'))
        sub = df[[var_x, var_y]].dropna()
        if sub.empty:
            return None
        if by_var and by_var in df.columns:
            sub = df[[var_x, var_y, by_var]].dropna()
            fig = go.Figure()
            for val in sub[by_var].unique():
                mask = sub[by_var] == val
                label = lm.format_value(by_var, val) if lm else str(val)
                fig.add_trace(go.Scatter(
                    x=sub.loc[mask, var_x], y=sub.loc[mask, var_y],
                    mode='markers', name=str(label)
                ))
        else:
            fig = go.Figure(data=[go.Scatter(x=sub[var_x], y=sub[var_y], mode='markers',
                                              marker=dict(color=PLOTLY_DEFAULT_GREEN))])
        if show_lfit and len(sub) >= 2:
            try:
                x_num = pd.to_numeric(sub[var_x], errors='coerce')
                y_num = pd.to_numeric(sub[var_y], errors='coerce')
                valid = x_num.notna() & y_num.notna()
                if valid.sum() >= 2:
                    coeffs = _np.polyfit(x_num[valid], y_num[valid], 1)
                    x_line = _np.linspace(float(x_num[valid].min()), float(x_num[valid].max()), 100)
                    y_line = _np.polyval(coeffs, x_line)
                    fig.add_trace(go.Scatter(
                        x=x_line.tolist(), y=y_line.tolist(),
                        mode='lines', line=dict(color='red', width=2, dash='dash'),
                        name=f'Lfit (β={coeffs[0]:.3f})'
                    ))
            except Exception:
                pass
        fig.update_layout(
            template='plotly_white',
            margin=dict(l=50, r=50, t=40, b=60),
            xaxis_title=var_x,
            yaxis_title=var_y
        )
        return fig

    def _piechart(self, df, args, options):
        import plotly.graph_objects as go

        if 'raw' in args or not args.get('vars'):
            return None
        var = args['vars'][0]
        if var not in df.columns:
            return None
        stat = args.get('stat', 'count').lower()
        s = df[var].value_counts(dropna=False).sort_index()
        if s.empty:
            return None
        labels = self._format_labels(options, var, s.index.tolist())
        if stat == 'percent':
            values = (s / s.sum() * 100).round(1).tolist()
        else:
            values = s.values.tolist()
        fig = go.Figure(data=[go.Pie(labels=labels, values=values, hole=0)])
        fig.update_layout(template='plotly_white', margin=dict(l=50, r=50, t=40, b=60))
        return fig

    def _hexbin(self, df, args, options):
        """2D tetthetsplott (hexbin-stil) – bruker Histogram2d for plotly-kompatibilitet."""
        import plotly.graph_objects as go

        if 'raw' in args or len(args.get('vars', [])) < 2:
            return None
        var_x, var_y = args['vars'][0], args['vars'][1]
        if var_x not in df.columns or var_y not in df.columns:
            return None
        sub = df[[var_x, var_y]].dropna()
        if sub.empty or len(sub) < 2:
            return None
        nbins = options.get('bin') or options.get('nbins')
        try:
            n = int(nbins) if nbins else 30
        except (ValueError, TypeError):
            n = 30
        fig = go.Figure(data=[go.Histogram2d(
            x=sub[var_x], y=sub[var_y],
            nbinsx=n, nbinsy=n,
            colorscale='Blues',
            showscale=True
        )])
        fig.update_layout(
            template='plotly_white',
            margin=dict(l=50, r=50, t=40, b=60),
            xaxis_title=var_x,
            yaxis_title=var_y
        )
        return fig

    def _sankey(self, df, args, options):
        """Sankey-diagram: overganger mellom kategoriske variabler (var1->var2->var3...)."""
        import plotly.graph_objects as go

        if 'raw' in args or len(args.get('vars', [])) < 2:
            return None
        vars_list = [v for v in args['vars'] if v in df.columns]
        if len(vars_list) < 2:
            return None
        sub = df[vars_list].dropna(how='any')
        if sub.empty:
            return None

        lm = options.get('_label_manager')

        # Først: samle unike verdier per stage (én node per stage+verdi)
        stages = []
        stage_indices = []
        offsets = [0]
        for va in vars_list:
            uniq = sub[va].dropna().unique().tolist()
            stages.append(uniq)
            stage_indices.append({v: offsets[-1] + j for j, v in enumerate(uniq)})
            offsets.append(offsets[-1] + len(uniq))

        all_labels = []
        for i, (va, uniq) in enumerate(zip(vars_list, stages)):
            for v in uniq:
                lbl = lm.format_value(va, v) if lm else str(v)
                all_labels.append(lbl)

        links_sources = []
        links_targets = []
        links_values = []

        for i in range(len(vars_list) - 1):
            va, vb = vars_list[i], vars_list[i + 1]
            grp = sub.groupby([va, vb], dropna=False).size().reset_index(name='count')
            if grp.empty:
                continue
            idx_a, idx_b = stage_indices[i], stage_indices[i + 1]
            for _, row in grp.iterrows():
                a_val, b_val, cnt = row[va], row[vb], row['count']
                if pd.isna(a_val) or pd.isna(b_val):
                    continue
                src = idx_a.get(a_val)
                tgt = idx_b.get(b_val)
                if src is not None and tgt is not None:
                    links_sources.append(src)
                    links_targets.append(tgt)
                    links_values.append(int(cnt))

        if not links_values:
            return None

        fig = go.Figure(data=[go.Sankey(
            node=dict(
                label=all_labels,
                pad=15,
                thickness=20
            ),
            link=dict(
                source=links_sources,
                target=links_targets,
                value=links_values
            )
        )])
        fig.update_layout(
            template='plotly_white',
            margin=dict(l=20, r=20, t=40, b=20),
            height=400
        )
        return fig


class MicroInterpreter:
    def __init__(self, metadata_path=None, catalog=None, echo_commands: bool = True, metadata_base_url=None):
        self.datasets = {}
        self.dataset_entity_types = {}   # datasett-navn -> 'person' | 'episode_npr'
        self.dataset_key_cols: dict = {}  # datasett-navn -> nøkkelkolonne etter collapse
        self.active_name = None
        self.parser = MicroParser()
        self.data_engine = MockDataEngine(metadata_path=metadata_path, catalog=catalog)
        if metadata_base_url:
            u = str(metadata_base_url).strip()
            self.data_engine._page_base_url = u if u.endswith('/') else (u + '/')
        self.label_manager = LabelManager(catalog=getattr(self.data_engine, 'catalog', {}))
        self.stats_engine = StatsEngine()
        self.transform_handler = DataTransformHandler(label_manager=self.label_manager)
        self.reg_engine = RegressionHandler()
        self.survival_handler = SurvivalHandler()
        self.plot_handler = PlotHandler()
        self.output_log = []
        self.bindings = {}  # let: name -> value (skalar: tall, streng)
        self.echo_commands = echo_commands
        self._config = {'alpha': 0.05, 'seed': None, 'cache': True}
        self.default_decimals = 2
        self._apply_float_format()
        self._command_history = []
        # Sett globale referanser for label- og bind()-funksjoner i eval
        set_label_manager(self.label_manager)
        set_bindings(self.bindings)

    def _apply_float_format(self):
        """Sett pandas float-format basert på default_decimals (smart: ekstra desimaler for små tall)."""
        dec = int(self.default_decimals)
        pd.options.display.float_format = lambda x: _smart_float_fmt(x, dec)

    def sync_datasets_to_globals(self, g):
        """Binder self.datasets til et exec-globals dict (Pyodide): datasets, active_name, active_df, og ett navn per gyldig identifier."""
        g["datasets"] = self.datasets
        g["active_name"] = self.active_name
        for dn, df in list(self.datasets.items()):
            dsn = str(dn)
            if dsn.isidentifier():
                g[dsn] = df
        an = self.active_name
        if an and an in self.datasets:
            g["active_df"] = self.datasets[an]
            g["df"] = self.datasets[an]
        else:
            g["active_df"] = None
            g["df"] = None

    def _eval_let_expression(self, expr):
        """Evaluerer let-uttrykk: 'streng', tall, $ref, eller a ++ b ++ ..."""
        expr = expr.strip()
        parts = [p.strip() for p in re.split(r'\s*\+\+\s*', expr)]
        results = []
        for p in parts:
            if not p:
                continue
            if p.startswith('$'):
                name = p[1:]
                if name in self.bindings:
                    results.append(str(self.bindings[name]))
                else:
                    results.append(p)  # Ukjent binding: behold som streng
            elif (p.startswith("'") and p.endswith("'")) or (p.startswith('"') and p.endswith('"')):
                results.append(p[1:-1])
            else:
                try:
                    results.append(str(int(p)) if '.' not in p else str(float(p)))
                except ValueError:
                    # Prøv aritmetisk eval (f.eks. $a + 1 etter binding-substitusjon)
                    try:
                        val = eval(p, _LET_EVAL_ENV)
                        if isinstance(val, float) and val == int(val):
                            results.append(str(int(val)))
                        elif isinstance(val, (int, float)):
                            results.append(str(val))
                        else:
                            results.append(str(val))
                    except Exception:
                        results.append(p)
        return ''.join(results) if results else expr

    def _parse_condition(self, cond):
        """Parser en enkel betingelse: var op value. Returnerer (var, op, value) eller None.
        op: ==, !=, <, >, <=, >=. value: tall eller streng (avkodet fra anførselstegn)."""
        if not cond or not cond.strip():
            return None
        cond = cond.strip()
        # Sammensatte uttrykk (Stata: &) må gjennom df.eval — ellers matcher første == feil (rest blir '1' & foo...)
        if '&' in cond or '|' in cond:
            return None
        # Operator først (lengste først for <=, >=, ==, !=)
        for op in ('==', '!=', '<=', '>=', '<', '>'):
            if op in cond:
                i = cond.index(op)
                var = cond[:i].strip()
                rest = cond[i + len(op):].strip()
                if not var or not rest:
                    return None
                # Parse verdi: anførselstegn eller tall
                if (rest.startswith('"') and rest.endswith('"')) or (rest.startswith("'") and rest.endswith("'")):
                    value = rest[1:-1]
                else:
                    value = rest.strip()
                    try:
                        if '.' in value:
                            value = float(value)
                        else:
                            value = int(value)
                    except ValueError:
                        pass
                return (var, op, value)
        return None

    def _resolve_condition_value(self, var, value, df, lm):
        """Resolver betingelsesverdi for variabel: label -> kode ved codelist; tilpass type til kolonnen.

        Returnerer (primærverdi, aux) der aux kan inneholde ekstra kodeformer:
        f.eks. {'int_code': 301, 'str_code': '0301'} for kommunevariabler.
        """
        aux = {}
        if var not in df.columns:
            return value, aux
        col = df[var]
        cl = lm.get_codelist_for_var(var) if lm else None

        # Hvis verdi er streng og matcher en label, bruk kode (labeltekst -> kode)
        if isinstance(value, str) and cl:
            for code, label in cl.items():
                if label == value:
                    aux['int_code'] = code
                    aux['str_code'] = str(code)
                    value = code
                    break
            # Ellers: hvis verdi er streng som ser ut som tall og finnes som kode
            if value in cl:
                aux.setdefault('int_code', value if not isinstance(value, str) else None)
            elif isinstance(value, str) and value.lstrip('-').replace('.', '', 1).isdigit():
                try:
                    vn = int(value) if '.' not in value else float(value)
                    if vn in cl:
                        aux['int_code'] = vn
                        aux.setdefault('str_code', value)
                        value = vn
                except ValueError:
                    pass

        # Tilpass type til kolonnen slik at primærsammenligning treffer (int-kolonne vs object-kolonne)
        if pd.api.types.is_numeric_dtype(col):
            try:
                if isinstance(value, (float, np.floating)) and value == int(value):
                    prim = int(value)
                else:
                    prim = float(value)
                aux.setdefault('int_code', prim if isinstance(prim, int) else None)
                return prim, aux
            except (ValueError, TypeError):
                return value, aux

        # Kolonne er object/string: bruk streng for sammenligning
        prim = value if isinstance(value, str) else str(value)
        if 'int_code' in aux and 'str_code' not in aux:
            aux['str_code'] = str(aux['int_code'])
        return prim, aux

    def _eval_condition_mask(self, df, cond):
        """Bygger boolsk mask fra betingelse. Støtter ==, !=, <, >, <=, >= og label-oppslag.
        Returnerer pandas Series (mask) eller None ved parsing-feil (da kan caller falle tilbake til query)."""
        parsed = self._parse_condition(cond)
        if not parsed:
            return None
        var, op, value = parsed
        if var not in df.columns:
            return None
        # Kolonne-til-kolonne-sammenligning: hvis RHS er en streng som matcher et kolonnenavn,
        # bruk den kolonnens verdier i stedet for en streng-literal.
        if isinstance(value, str) and value in df.columns:
            col_l = df[var]
            col_r = df[value]
            try:
                if op == '==':
                    return (col_l == col_r).fillna(False)
                elif op == '!=':
                    return (col_l != col_r).fillna(False)
                elif op == '<':
                    return (col_l < col_r).fillna(False)
                elif op == '>':
                    return (col_l > col_r).fillna(False)
                elif op == '<=':
                    return (col_l <= col_r).fillna(False)
                elif op == '>=':
                    return (col_l >= col_r).fillna(False)
            except Exception:
                return None
        resolved, aux = self._resolve_condition_value(var, value, df, self.label_manager)
        col = df[var]
        # Sammenligning som fungerer for både numerisk og object
        try:
            if op == '==':
                # Først: direkte sammenligning
                mask = (col == resolved)
                # Hvis ingen treff og kolonnen er object/str, prøv streng-varianter
                if not mask.any() and pd.api.types.is_object_dtype(col):
                    candidates = [str(resolved)]
                    if 'str_code' in aux:
                        candidates.append(str(aux['str_code']))
                    if 'int_code' in aux:
                        candidates.append(str(aux['int_code']))
                    for cand in candidates:
                        m2 = (col.astype(str) == cand)
                        if m2.any():
                            mask = m2
                            break
            elif op == '!=':
                if pd.api.types.is_object_dtype(col):
                    # Ulikhet: bruk samme kandidatlogikk som for likhet
                    candidates = [str(resolved)]
                    if 'str_code' in aux:
                        candidates.append(str(aux['str_code']))
                    if 'int_code' in aux:
                        candidates.append(str(aux['int_code']))
                    # Start med primærverdi
                    mask = (col.astype(str) != candidates[0])
                else:
                    mask = (col != resolved)
            elif op in ('<', '>', '<=', '>='):
                # For ordningssammenligning: konverter til numerisk der mulig
                c = col
                if pd.api.types.is_object_dtype(col):
                    try:
                        c = pd.to_numeric(col, errors='coerce')
                    except Exception:
                        c = col
                r = resolved
                if not isinstance(r, (int, float, np.number)):
                    try:
                        r = float(r) if isinstance(r, str) and '.' in r else int(r)
                    except (ValueError, TypeError):
                        return None
                if op == '<':
                    mask = c < r
                elif op == '>':
                    mask = c > r
                elif op == '<=':
                    mask = c <= r
                else:
                    mask = c >= r
            else:
                return None
            return mask
        except Exception:
            return None

    def _substitute_bindings(self, text):
        """Erstatt $name med bindings[name] i tekst. Ukjente beholdes som $name.
        Kollapser også `ident++ident` (streng-konkat) til sammenhengende identifikator.
        Inline date_fmt(y[,m[,d]]) med numeriske argumenter evalueres til YYYY-MM-DD."""
        if not isinstance(text, str):
            return text
        def repl(m):
            name = m.group(1)
            return str(self.bindings[name]) if name in self.bindings else m.group(0)
        text = re.sub(r'\$([\wøæåØÆÅ]+)', repl, text)
        # Kollaps `ident ++ ident` eller `ident++ident` til `identident` (microdata ++ konkat)
        text = re.sub(r'([\wøæåØÆÅ])\s*\+\+\s*(?=[\wøæåØÆÅ])', r'\1', text)
        # Inline date_fmt(y[,m[,d]]) → YYYY-MM-DD (kun når alle argumenter er heltallsliteraler)
        def _eval_date_fmt(m):
            args = [a.strip() for a in m.group(1).split(',')]
            try:
                y = int(args[0])
                mo = int(args[1]) if len(args) > 1 else 1
                d = int(args[2]) if len(args) > 2 else 1
                return f"{y:04d}-{mo:02d}-{d:02d}"
            except (ValueError, IndexError):
                return m.group(0)
        text = re.sub(r'date_fmt\(([^)]+)\)', _eval_date_fmt, text)
        return text

    @property
    def active_df(self):
        if not self.active_name:
            raise ValueError("Ingen aktivt datasett. Bruk 'create-dataset' eller 'use'.")
        return self.datasets[self.active_name]

    def run_script(self, script_text, echo_commands=None):
        script_text = self.parser.preprocess_script(script_text)
        lines = script_text.split('\n')
        echo = self.echo_commands if echo_commands is None else bool(echo_commands)
        # Callback for å yield mellom kommandoer (Pyodide: lar nettleseren oppdatere UI)
        _yield_fn = getattr(self, '_yield_callback', None)
        i = 0
        while i < len(lines):
            raw_line = lines[i]
            line = self._substitute_bindings(raw_line)
            instr = self.parser.parse_line(line)
            if not instr:
                i += 1
                continue
            cmd = instr['command']
            args = instr['args']
            # Spor kommandohistorikk (ikke textblock/endblock/kommentarer)
            stripped_raw = raw_line.strip()
            if stripped_raw and cmd not in ('textblock', 'endblock'):
                self._command_history.append(stripped_raw)
            if echo and cmd not in ('textblock', 'endblock'):
                stripped = raw_line.strip()
                if stripped:
                    prefix = f"{self.active_name} >> " if self.active_name else ">> "
                    self._log(prefix + stripped, indent=False)

            # for/end: samle løkkebody og iterer
            if cmd == 'for' and isinstance(args, dict) and 'var' in args and 'values' in args:
                body_lines = []
                j = i + 1
                while j < len(lines):
                    sub_line = self._substitute_bindings(lines[j])
                    sub_instr = self.parser.parse_line(sub_line)
                    if sub_instr and sub_instr['command'] == 'end':
                        break
                    body_lines.append(lines[j])
                    j += 1
                for val in args['values']:
                    self.bindings[args['var']] = val
                    for bl in body_lines:
                        bl_sub = self._substitute_bindings(bl)
                        bi = self.parser.parse_line(bl_sub)
                        if bi and bi['command'] != 'end':
                            self._execute_instruction(bi)
                    if _yield_fn:
                        _yield_fn()
                i = j + 1
                continue
            if cmd == 'end':
                i += 1
                continue

            # textblock/endblock: samle tekst, vis som markdown (ikke eksekvert)
            if cmd == 'textblock':
                body_lines = []
                j = i + 1
                while j < len(lines):
                    if lines[j].strip().lower() == 'endblock':
                        break
                    body_lines.append(lines[j])
                    j += 1
                content = "\n".join(body_lines).strip()
                if content:
                    self._log_embed('markdown', content)
                i = j + 1
                continue
            if cmd == 'endblock':
                i += 1
                continue

            self._execute_instruction(instr)
            if echo:
                self._log("")
            i += 1

        return "\n".join(self.output_log)

    async def run_script_async(self, script_text, echo_commands=None):
        """Async versjon av run_script som yielder mellom kommandoer (for Pyodide/nettleser)."""
        import asyncio
        script_text = self.parser.preprocess_script(script_text)
        lines = script_text.split('\n')
        echo = self.echo_commands if echo_commands is None else bool(echo_commands)
        _cmd_count = 0
        i = 0
        while i < len(lines):
            raw_line = lines[i]
            line = self._substitute_bindings(raw_line)
            instr = self.parser.parse_line(line)
            if not instr:
                i += 1
                continue
            cmd = instr['command']
            args = instr['args']
            stripped_raw = raw_line.strip()
            if stripped_raw and cmd not in ('textblock', 'endblock'):
                self._command_history.append(stripped_raw)
            if echo and cmd not in ('textblock', 'endblock'):
                stripped = raw_line.strip()
                if stripped:
                    prefix = f"{self.active_name} >> " if self.active_name else ">> "
                    self._log(prefix + stripped, indent=False)

            if cmd == 'for' and isinstance(args, dict) and 'var' in args and 'values' in args:
                body_lines = []
                j = i + 1
                while j < len(lines):
                    sub_line = self._substitute_bindings(lines[j])
                    sub_instr = self.parser.parse_line(sub_line)
                    if sub_instr and sub_instr['command'] == 'end':
                        break
                    body_lines.append(lines[j])
                    j += 1
                for val in args['values']:
                    self.bindings[args['var']] = val
                    for bl in body_lines:
                        bl_sub = self._substitute_bindings(bl)
                        bi = self.parser.parse_line(bl_sub)
                        if bi and bi['command'] != 'end':
                            self._execute_instruction(bi)
                            _cmd_count += 1
                    # Yield til nettleseren etter hver loop-iterasjon
                    await asyncio.sleep(0)
                i = j + 1
                continue
            if cmd == 'end':
                i += 1
                continue
            if cmd == 'textblock':
                body_lines = []
                j = i + 1
                while j < len(lines):
                    if lines[j].strip().lower() == 'endblock':
                        break
                    body_lines.append(lines[j])
                    j += 1
                content = "\n".join(body_lines).strip()
                if content:
                    self._log_embed('markdown', content)
                i = j + 1
                continue
            if cmd == 'endblock':
                i += 1
                continue

            self._execute_instruction(instr)
            if echo:
                self._log("")
            _cmd_count += 1
            # Yield til nettleseren ca. hver 5. kommando
            if _cmd_count % 5 == 0:
                await asyncio.sleep(0)
            i += 1

        return "\n".join(self.output_log)

    def translate_script_to_python(self, script_text):
        """Oversetter microdata-script til ekvivalent Python-kode (uten å kjøre)."""
        script_text = self.parser.preprocess_script(script_text)
        lines = script_text.split('\n')
        out = []
        out.append('# Generert Python fra microdata-script')
        out.append('import pandas as pd')
        out.append('import numpy as np')
        out.append('')
        active_name = None
        i = 0
        while i < len(lines):
            raw_line = lines[i]
            line = self._substitute_bindings(raw_line)
            instr = self.parser.parse_line(line)
            if not instr:
                i += 1
                continue
            cmd = instr['command']
            args = instr['args']
            opts = instr.get('options') or {}

            if cmd == 'for' and isinstance(args, dict) and 'var' in args and 'values' in args:
                body_lines = []
                j = i + 1
                while j < len(lines):
                    sub_instr = self.parser.parse_line(self._substitute_bindings(lines[j]))
                    if sub_instr and sub_instr['command'] == 'end':
                        break
                    body_lines.append(lines[j])
                    j += 1
                var = args['var']
                vals = args['values']
                out.append(f'# for {var} in {vals}')
                out.append(f'for {var} in {repr(vals)}:')
                for bl in body_lines:
                    sub_instr = self.parser.parse_line(self._substitute_bindings(bl))
                    if not sub_instr or sub_instr['command'] == 'end':
                        continue
                    for py_line in self._emit_python_instruction(sub_instr, active_name):
                        out.append('    ' + py_line)
                i = j + 1
                continue
            if cmd == 'end':
                i += 1
                continue
            if cmd == 'textblock':
                j = i + 1
                while j < len(lines) and lines[j].strip().lower() != 'endblock':
                    j += 1
                out.append('# textblock (markdown)')
                i = j + 1
                continue
            if cmd == 'endblock':
                i += 1
                continue

            for py_line in self._emit_python_instruction(instr, active_name):
                out.append(py_line)
            if cmd == 'create-dataset' and isinstance(args, (list, tuple)) and len(args) > 0:
                active_name = args[0]
            elif cmd == 'use' and isinstance(args, (list, tuple)) and len(args) > 0:
                active_name = args[0]
            i += 1
        return '\n'.join(out)

    def _emit_python_instruction(self, instr, active_name):
        """Returnerer liste med Python-linjer for én microdata-instruksjon."""
        cmd = instr['command']
        args = instr['args']
        opts = instr.get('options') or {}
        cond = instr.get('condition')
        raw = args.get('raw') if isinstance(args, dict) else None
        if isinstance(args, (list, tuple)) and len(args) == 0:
            args = []
        elif isinstance(args, (list, tuple)):
            pass
        else:
            args = args if isinstance(args, dict) else {}

        lines = []
        comment = f'# {cmd}'

        if cmd == 'create-dataset':
            name = args[0] if isinstance(args, (list, tuple)) else args.get('name', 'df')
            lines.append(f'{comment}')
            lines.append(f"df_{name} = pd.DataFrame()")
            lines.append(f"active_df = df_{name}  # aktivt datasett")
            return lines
        if cmd == 'use':
            name = args[0] if isinstance(args, (list, tuple)) and len(args) > 0 else args.get('name', 'df')
            lines.append(f'{comment}')
            lines.append(f"active_df = df_{name}")
            return lines
        if cmd == 'let':
            if isinstance(args, dict) and 'name' in args and 'expression' in args:
                lines.append(f'{comment} {args["name"]} = {args["expression"]}')
                try:
                    val = self._eval_let_expression(args['expression'])
                    lines.append(f"{args['name']} = {repr(val)}  # evaluert ved oversettelse")
                except Exception:
                    lines.append(f"# {args['name']} = <uttrykk: {args['expression']}>")
            return lines
        if cmd == 'require':
            lines.append(f'{comment} (no-op i denne kjøringen)')
            return lines
        if cmd in ['import', 'import-event']:
            if isinstance(args, dict):
                var = args.get('var', '')
                alias = args.get('alias') or (var.split('/')[-1] if var else '')
                date1 = args.get('date1', '')
                lines.append(f'{comment} {var} as {alias}')
                lines.append(f"# data_engine.generate({repr(cmd)}, ...) -> merge på unit_id")
                lines.append(f"# active_df = pd.merge(active_df, new_data, on='unit_id', how='left')")
            return lines
        if cmd == 'import-panel':
            if isinstance(args, dict) and 'vars' in args and 'dates' in args:
                lines.append(f'{comment} vars={args["vars"]} dates={args["dates"]}')
                lines.append(f"# import-panel: paneldata fra flere tidspunkter")
            return lines
        if cmd == 'define-labels':
            if isinstance(args, dict) and 'name' in args and 'pairs' in args:
                pairs = args['pairs']
                lines.append(f'{comment} {args["name"]} ' + ' '.join(f'{k} {v}' for k, v in pairs))
                lines.append(f"# label_manager.define_labels({repr(args['name'])}, ...)")
            return lines
        if cmd == 'assign-labels':
            if isinstance(args, dict) and 'var' in args and 'codelist' in args:
                lines.append(f'{comment} {args["var"]} -> {args["codelist"]}')
                lines.append(f"# label_manager.assign_labels({repr(args['var'])}, {repr(args['codelist'])})")
            return lines
        if cmd == 'drop-labels':
            lines.append(f'{comment}')
            lines.append(f"# label_manager.drop_labels(...)")
            return lines
        if cmd == 'list-labels':
            lines.append(f'{comment}')
            lines.append(f"# label_manager.list_labels_output(...)")
            return lines
        if cmd == 'clone-dataset':
            a, b = (args[0], args[1]) if isinstance(args, (list, tuple)) and len(args) >= 2 else (None, None)
            if a and b:
                lines.append(f'{comment} {a} -> {b}')
                lines.append(f"df_{b} = df_{a}.copy(deep=True)")
            return lines
        if cmd == 'delete-dataset':
            name = args[0] if isinstance(args, (list, tuple)) else None
            if name:
                lines.append(f'{comment} {name}')
                lines.append(f"del df_{name}")
            return lines
        if cmd == 'rename-dataset':
            if isinstance(args, (list, tuple)) and len(args) >= 2:
                lines.append(f'{comment} {args[0]} -> {args[1]}')
                lines.append(f"df_{args[1]} = df_{args[0]}; del df_{args[0]}")
            return lines
        if cmd == 'merge':
            t = args[0] if isinstance(args, (list, tuple)) else None
            how = 'outer' if opts.get('outer_join') else 'left'
            if t:
                lines.append(f'{comment} {t} (how={how})')
                lines.append(f"active_df = pd.merge(active_df, df_{t}, on='unit_id', how={repr(how)})")
            return lines
        if cmd == 'variables':
            lines.append(f'{comment}')
            lines.append(f"# [c for c in active_df.columns if c not in ('unit_id', 'PERSONID_1', 'tid')]")
            return lines
        if cmd == 'generate':
            if isinstance(args, dict) and 'target' in args and 'expression' in args:
                cond_s = f" if {cond}" if cond else ""
                lines.append(f'{comment} {args["target"]} = {args["expression"]}{cond_s}')
                lines.append(f"active_df['{args['target']}'] = active_df.eval({repr(args['expression'])})")
            return lines
        if cmd == 'aggregate':
            if isinstance(args, dict) and 'targets' in args:
                for t in args['targets']:
                    stat, src, target = t.get('stat'), t.get('src'), t.get('target')
                    lines.append(f'{comment} ({stat}) {src} -> {target}')
                lines.append(f"# agg = active_df.groupby(...).agg(...); active_df = pd.merge(active_df, agg, ...)")
            return lines
        if cmd == 'sample':
            if isinstance(args, dict) and 'raw' not in args:
                c = args.get('count'); f = args.get('fraction'); s = args.get('seed')
                lines.append(f'{comment} count/fraction={c or f} seed={s}')
                lines.append(f"rng = np.random.default_rng({s}); idx = rng.choice(active_df.index, size=..., replace=False); active_df = active_df.loc[idx]")
            return lines
        if cmd == 'summarize':
            vars_list = args if isinstance(args, (list, tuple)) else (args.get('vars', []) if isinstance(args, dict) else [])
            if vars_list:
                lines.append(f'{comment} ' + ' '.join(vars_list))
                by = opts.get('by')
                if by:
                    lines.append(f"# active_df.groupby({repr(by)})[vars].describe() eller .agg(...)")
                else:
                    lines.append(f"# active_df[vars].describe() eller gini/iqr via functions")
            return lines
        if cmd == 'tabulate':
            vars_list = args if isinstance(args, (list, tuple)) else (args.get('vars', []) if isinstance(args, dict) else [])
            lines.append(f'{comment} ' + ' '.join(str(v) for v in vars_list) + (' ' + str(opts) if opts else ''))
            lines.append(f"# pd.crosstab(...) eller stats_engine med labels")
            return lines
        if cmd == 'correlate':
            vars_list = args if isinstance(args, (list, tuple)) else (args.get('vars', []) if isinstance(args, dict) else [])
            lines.append(f'{comment} ' + ' '.join(str(v) for v in vars_list))
            lines.append(f"# active_df[vars].corr() eller pearsonr")
            return lines
        if cmd == 'ci':
            vars_list = args if isinstance(args, (list, tuple)) else (args.get('vars', []) if isinstance(args, dict) else [])
            lines.append(f'{comment} ' + ' '.join(str(v) for v in vars_list) + (' ' + str(opts) if opts else ''))
            lines.append(f"# Konfidensintervall (t eller norm)")
            return lines
        if cmd == 'anova':
            vars_list = args if isinstance(args, (list, tuple)) else (args.get('vars', []) if isinstance(args, dict) else [])
            lines.append(f'{comment} ' + ' '.join(str(v) for v in vars_list))
            lines.append(f"# statsmodels anova_lm(ols(...))")
            return lines
        if cmd in ['regress', 'logit', 'probit', 'poisson', 'regress-predict', 'probit-predict', 'logit-predict', 'mlogit', 'mlogit-predict', 'ivregress', 'ivregress-predict', 'regress-panel-predict', 'regress-panel-diff', 'rdd']:
            vars_list = args if isinstance(args, (list, tuple)) else (args.get('vars', []) if isinstance(args, dict) else [])
            lines.append(f'{comment} ' + ' '.join(str(v) for v in vars_list) + (' ' + str(opts) if opts else ''))
            lines.append(f"# statsmodels OLS/Logit/Probit/Poisson eller regress-predict pred/residuals")
            return lines
        if cmd == 'regress-panel':
            lines.append(f'{comment}')
            lines.append(f"# linearmodels PanelOLS/RandomEffects/BetweenOLS, eller statsmodels (within-FE, MixedLM-RE, between-OLS)")
            return lines
        if cmd in ['barchart', 'histogram', 'boxplot', 'scatter', 'piechart', 'hexbin', 'sankey']:
            vars_list = args if isinstance(args, (list, tuple)) else (args.get('vars', []) if isinstance(args, dict) else [])
            lines.append(f'{comment} ' + ' '.join(str(v) for v in vars_list))
            lines.append(f"# plot_handler.execute() -> plotly fig")
            return lines
        if cmd in ['cox', 'kaplan-meier', 'kaplan_meier', 'weibull']:
            vars_list = args if isinstance(args, (list, tuple)) else (args.get('vars', []) if isinstance(args, dict) else [])
            lines.append(f'{comment} ' + ' '.join(str(v) for v in vars_list))
            lines.append(f"# lifelines CoxPHFitter / KaplanMeierFitter / WeibullAFTFitter")
            return lines
        if cmd in ['rename', 'replace', 'drop', 'keep', 'clone-variables', 'destring', 'recode']:
            lines.append(f'{comment} {args if not isinstance(args, dict) else args}')
            lines.append(f"# transform_handler.execute() -> ny active_df")
            return lines
        if cmd in ['collapse', 'summarize-panel', 'tabulate-panel', 'normaltest', 'transitions-panel']:
            lines.append(f'{comment} ...')
            lines.append(f"# stats_engine / plot_handler")
            return lines
        if cmd == 'hausman':
            lines.append(f'{comment}')
            lines.append(f"# linearmodels FE vs RE, eller statsmodels (within + MixedLM)")
            return lines
        lines.append(f'{comment} (ukjent kommando)')
        return lines

    def _execute_instruction(self, instr):
        cmd = instr['command']
        args = instr['args']
        opts = instr['options']
        cond = instr['condition']

        try:
            # 1. Globale/Sesjons-kommandoer
            if cmd == 'create-dataset':
                self.datasets[args[0]] = pd.DataFrame()
                self.active_name = args[0]
                self.dataset_entity_types.pop(args[0], None)
                self.dataset_key_cols.pop(args[0], None)
                # Tilnærmet microdata-tekst:
                # "Et tomt datasett, X, ble opprettet og valgt"
                self._log(f"Et tomt datasett, {args[0]}, ble opprettet og valgt")
                return

            if cmd == 'use':
                self.active_name = args[0]
                # "Datasettet X er valgt"
                self._log(f"Datasettet {args[0]} er valgt")
                return

            if cmd == 'require':
                # Tilnærmet microdata-tekst:
                # "Opprettet en kobling fra <kilde> til <alias>"
                parts = args.split() if isinstance(args, str) else list(args)
                source = parts[0] if parts else ''
                alias = parts[-1] if len(parts) >= 3 and parts[-2].lower() == 'as' else (parts[-1] if parts else '')
                if source and alias:
                    self._log(f"Opprettet en kobling fra {source} til {alias}")
                else:
                    self._log("Opprettet en (lokal) kobling")
                return  # No-op mot faktisk SSB, kun logg for kompatibilitet

            if cmd == 'clone-dataset':
                self.datasets[args[1]] = self.datasets[args[0]].copy(deep=True)
                self._log(f"Kopierte datasett {args[0]} til {args[1]}")
                return

            if cmd == 'clone-units':
                _ck = _get_df_key_col(self.datasets[args[0]]) or 'unit_id'
                self.datasets[args[1]] = self.datasets[args[0]][[_ck]].drop_duplicates()
                return

            if cmd == 'delete-dataset':
                if isinstance(args, dict) and 'raw' in args:
                    self._log("FEIL: delete-dataset krever datasettnavn.")
                    return
                name = args[0]
                if name not in self.datasets:
                    self._log(f"FEIL: Datasett '{name}' finnes ikke.")
                    return
                del self.datasets[name]
                if self.active_name == name:
                    self.active_name = next((n for n in self.datasets), None)
                self._log(f"Slettet datasett: {name}")
                return

            if cmd == 'rename-dataset':
                if isinstance(args, dict) and 'raw' in args:
                    self._log("FEIL: rename-dataset krever gammelt og nytt navn.")
                    return
                old_name, new_name = args[0], args[1]
                if old_name not in self.datasets:
                    self._log(f"FEIL: Datasett '{old_name}' finnes ikke.")
                    return
                if new_name in self.datasets and new_name != old_name:
                    self._log(f"FEIL: Datasett '{new_name}' finnes allerede.")
                    return
                self.datasets[new_name] = self.datasets.pop(old_name)
                if self.active_name == old_name:
                    self.active_name = new_name
                self._log(f"Omdøpte datasett '{old_name}' til '{new_name}'")
                return

            if cmd == 'merge':
                # --- Ny syntaks: merge var-list into dataset [on variable] ---
                if isinstance(args, dict) and 'into' in args:
                    into_name = args['into']
                    var_list  = args.get('vars') or []
                    on_var    = args.get('on')

                    if into_name not in self.datasets:
                        self._log(f"FEIL: Datasett '{into_name}' finnes ikke.")
                        return

                    source_df = self.active_df
                    target_df = self.datasets[into_name]

                    src_key = (
                        _get_df_key_col(source_df)
                        or self.dataset_key_cols.get(self.active_name)
                    )
                    if src_key and src_key not in source_df.columns:
                        src_key = None
                    if src_key is None:
                        src_key = source_df.columns[0] if len(source_df.columns) > 0 else 'unit_id'
                    tgt_key = _get_df_key_col(target_df) or 'unit_id'

                    if on_var:
                        in_src = on_var in source_df.columns
                        in_tgt = on_var in target_df.columns
                        if in_src and in_tgt:
                            left_on, right_on = on_var, on_var
                        elif in_tgt:
                            # on_var finnes bare i målet — kobler mot kildedatasettets nøkkel
                            if src_key not in source_df.columns:
                                self._log(
                                    f"FEIL: '{on_var}' finnes i {into_name}, men ikke i {self.active_name}. "
                                    f"Kilden {self.active_name} har heller ikke nøkkelkolonnen '{src_key}'. "
                                    f"Tilgjengelige kolonner i {self.active_name}: {list(source_df.columns)}. "
                                    f"Bruk 'on <koblingsvariabel>' der koblingsvariabelen finnes i begge datasett."
                                )
                                return
                            left_on, right_on = on_var, src_key
                        elif in_src:
                            left_on, right_on = tgt_key, on_var   # target.tgt_key == source.on_var
                        else:
                            self._log(f"FEIL: Koblingsvariabel '{on_var}' finnes ikke i noen av datasettene.")
                            return
                    else:
                        if src_key in target_df.columns:
                            left_on = right_on = src_key
                        elif tgt_key in source_df.columns:
                            left_on = right_on = tgt_key
                        else:
                            common = list(set(source_df.columns) & set(target_df.columns))
                            if not common:
                                # Auto-detect FNR-to-PERSONID_1 linkage:
                                # If source OR target has a collapse key that's a person-ref FNR variable,
                                # match it against PERSONID_1 on the other side.
                                _src_collapse_key = self.dataset_key_cols.get(self.active_name)
                                _tgt_collapse_key = self.dataset_key_cols.get(into_name)
                                _fnr_matched = False

                                def _is_person_ref(alias):
                                    if not alias:
                                        return False
                                    reg = self.label_manager.var_alias_to_path.get(alias, '')
                                    return reg in _PERSONID_REF_VARS or reg.endswith('_FNR')

                                # Source has FNR-type collapse key → match vs target's PERSONID_1
                                if _src_collapse_key and _src_collapse_key in source_df.columns and _is_person_ref(_src_collapse_key):
                                    _pid_col = _get_df_key_col(target_df)
                                    if _pid_col and _pid_col in target_df.columns:
                                        left_on, right_on = _pid_col, _src_collapse_key
                                        _fnr_matched = True
                                # Target has FNR-type collapse key → match source's PERSONID_1 vs target's key
                                elif _tgt_collapse_key and _tgt_collapse_key in target_df.columns and _is_person_ref(_tgt_collapse_key):
                                    _pid_col = _get_df_key_col(source_df)
                                    if _pid_col and _pid_col in source_df.columns:
                                        left_on, right_on = _tgt_collapse_key, _pid_col
                                        _fnr_matched = True
                                if not _fnr_matched:
                                    _collapse_key = _src_collapse_key
                                    _hint = (
                                        f" Kilden '{self.active_name}' ble laget med collapse by({_collapse_key}). "
                                        f"Hvis '{_collapse_key}' finnes i {into_name}, bruk: merge ... into {into_name} on {_collapse_key}"
                                    ) if _collapse_key else (
                                        f" Kolonner i {self.active_name}: {list(source_df.columns)}. "
                                        f"Kolonner i {into_name}: {list(target_df.columns)}."
                                    )
                                    self._log(f"FEIL: Finner ingen felles koblingsvariabel mellom datasettene.{_hint}")
                                    return
                            else:
                                left_on = right_on = common[0]

                    cols_from_source = [c for c in var_list if c in source_df.columns]
                    if not cols_from_source:
                        missing = [c for c in var_list if c not in source_df.columns]
                        self._log(f"FEIL: {missing} finnes ikke i {self.active_name}.")
                        return

                    right_cols = list(dict.fromkeys([right_on] + cols_from_source))
                    right_df   = source_df[right_cols].drop_duplicates(subset=[right_on])

                    if left_on == right_on:
                        merged = pd.merge(target_df, right_df, on=left_on, how='left')
                    else:
                        # Sjekk om target allerede har right_on som egen kolonne
                        # (da får vi name-kollisjon og pandas auto-suffikser)
                        target_has_right_on = right_on in target_df.columns
                        merged = pd.merge(
                            target_df, right_df,
                            left_on=left_on, right_on=right_on,
                            how='left', suffixes=('', '_src_dup'),
                        )
                        # Drop duplikat-kolonner som ble suffikset fra kilde-siden
                        merged = merged.drop(columns=[c for c in merged.columns if c.endswith('_src_dup')])
                        # Drop right_on-kolonnen fra kilden KUN hvis target ikke hadde right_on.
                        # Hvis target hadde right_on, er den nåværende kolonnen target sin (ønsket)
                        # og source sin ble allerede droppet via _src_dup suffikset.
                        if not target_has_right_on and right_on != left_on and right_on in merged.columns:
                            merged = merged.drop(columns=[right_on])

                    self.datasets[into_name] = merged
                    n_str = f"{len(merged):,}".replace(",", " ")
                    self._log(f"Flettet {', '.join(cols_from_source)} fra {self.active_name} inn i {into_name} med {n_str} enheter")
                    return

                # --- Gammel syntaks: merge datasett-navn [, on(nøkkel)] ---
                target_df = self.datasets[args[0]]
                how = 'outer' if opts.get('outer_join') else 'left'
                _active_entity = self.dataset_entity_types.get(self.active_name, 'person')
                _default_key   = _ENTITY_ID_COL.get(_active_entity, 'unit_id')
                on_opt = opts.get('on', _default_key)
                on_cols = on_opt.split() if isinstance(on_opt, str) else list(on_opt)
                on_cols = [c for c in on_cols if c in self.active_df.columns and c in target_df.columns]
                if not on_cols:
                    on_cols = [c for c in [_default_key] if c in self.active_df.columns and c in target_df.columns]
                if not on_cols:
                    on_cols = list(set(self.active_df.columns) & set(target_df.columns))
                self.datasets[self.active_name] = pd.merge(self.active_df, target_df, on=on_cols, how=how)
                n_str = f"{len(self.datasets[self.active_name]):,}".replace(",", " ")
                self._log(f"Flettet variabler fra {args[0]} inn i {self.active_name} med {n_str} enheter")
                return

            # Label-kommandoer (krever ikke aktivt datasett)
            if cmd == 'define-labels':
                if 'name' in args and 'pairs' in args:
                    self.label_manager.define_labels(args['name'], args['pairs'])
                return
            if cmd == 'assign-labels':
                if 'var' in args and 'codelist' in args:
                    self.label_manager.assign_labels(args['var'], args['codelist'])
                return
            if cmd == 'drop-labels':
                if 'names' in args:
                    self.label_manager.drop_labels(*args['names'])
                return
            if cmd == 'list-labels':
                if 'codelist' in args:
                    out = self.label_manager.list_labels_output(args['codelist'], args.get('time'))
                    self._log(f"\n--- list-labels ---\n{out}\n")
                return

            # let: bindings (krever ikke aktivt datasett)
            if cmd == 'let' and 'name' in args and 'expression' in args:
                val = self._eval_let_expression(args['expression'])
                # Lagre som tall der mulig (støtter aritmetikk på bindings)
                try:
                    if isinstance(val, str) and '.' in val:
                        self.bindings[args['name']] = float(val)
                    elif isinstance(val, str) and val.lstrip('-').isdigit():
                        self.bindings[args['name']] = int(val)
                    else:
                        self.bindings[args['name']] = val
                except (ValueError, TypeError):
                    self.bindings[args['name']] = val
                return

            # configure: sett tolk-innstillinger
            if cmd == 'configure':
                args_list = args if isinstance(args, (list, tuple)) else []
                if args_list:
                    key = str(args_list[0]).lower()
                    if key == 'alpha' and len(args_list) >= 2:
                        try:
                            self._config['alpha'] = float(args_list[1])
                            self._log(f"Satt alpha = {self._config['alpha']}")
                        except ValueError:
                            self._log(f"FEIL: Ugyldig alpha-verdi: {args_list[1]}")
                    elif key == 'seed' and len(args_list) >= 2:
                        try:
                            self._config['seed'] = int(args_list[1])
                            self._log(f"Satt seed = {self._config['seed']}")
                        except ValueError:
                            self._log(f"FEIL: Ugyldig seed-verdi: {args_list[1]}")
                    elif key == 'nocache':
                        self._config['cache'] = False
                        self._log("Cache deaktivert")
                    elif key == 'cache':
                        self._config['cache'] = True
                        self._log("Cache aktivert")
                    else:
                        self._log(f"configure: ukjent innstilling '{key}'")
                return

            # history: vis liste over utførte kommandoer
            if cmd == 'history':
                hist = self._command_history
                lines_out = [f"\n--- Kommandohistorikk ({len(hist)} kommandoer) ---"]
                for idx, h in enumerate(hist[-50:], 1):
                    lines_out.append(f"  {idx:3d}: {h}")
                self._log("\n".join(lines_out) + "\n")
                return

            # help / help-function: kortfattet hjelp
            if cmd in ('help', 'help-function'):
                args_list = args if isinstance(args, (list, tuple)) else []
                topic = args_list[0] if args_list else ''
                if topic:
                    self._log(f"Hjelp for '{topic}': Se HTML-grensesnittet (microdata_runner.html) for fullstendig dokumentasjon.")
                else:
                    self._log("Bruk 'help <kommando>' eller 'help-function <funksjon>' for hjelp.")
                return

            # --- SIKRE AT VI HAR ET AKTIVT DATASETT HERFRA ---
            df_target = self.active_df

            # clear: tøm alle observasjoner i aktivt datasett
            if cmd == 'clear':
                self.datasets[self.active_name] = pd.DataFrame(columns=df_target.columns)
                self._log(f"Alle observasjoner i {self.active_name} er slettet")
                return

            # variables: vis variabler med type og kodeliste-info
            if cmd == 'variables':
                cols = [c for c in df_target.columns if c not in ('unit_id', 'PERSONID_1', 'tid')]
                try:
                    n_str = f"{len(df_target):,}".replace(",", " ")
                except Exception:
                    n_str = str(len(df_target))
                lines_out = [f"\n--- Variabler i {self.active_name} ({n_str} enheter) ---"]
                for col in cols:
                    dtype = df_target[col].dtype
                    type_str = 'numerisk' if pd.api.types.is_numeric_dtype(dtype) else 'tekst'
                    cl = self.label_manager.get_codelist_for_var(col) if self.label_manager else None
                    lbl_info = f' [{len(cl)} kodeverdier]' if cl else ''
                    lines_out.append(f"  {col:<30} {type_str}{lbl_info}")
                self._log("\n".join(lines_out) + "\n")
                return

            # 2. Transform-kommandoer (rename, replace, drop, keep, clone-variables, destring, recode)
            if cmd in ['rename', 'replace', 'drop', 'keep', 'clone-variables', 'destring', 'recode', 'reshape-to-panel', 'reshape-from-panel']:
                opts_copy = dict(opts)
                opts_copy['_condition'] = cond
                if cond:
                    opts_copy['_condition_mask'] = self._eval_condition_mask(df_target, cond)
                result = self.transform_handler.execute(cmd, df_target, args, opts_copy)
                if result is not None:
                    self.datasets[self.active_name] = result
                return

            # 2a. Sample (tilfeldig uttrekk) – sample count|fraction seed
            if cmd == 'sample' and 'raw' not in args:
                if cond:
                    mask = self._eval_condition_mask(df_target, cond)
                    if mask is not None:
                        df_src = df_target.loc[mask].copy()
                    else:
                        try:
                            df_src = df_target.loc[_py_eval_cond(df_target, cond)].copy()
                        except Exception:
                            df_src = df_target.query(cond).copy()
                else:
                    df_src = df_target
                if df_src.empty:
                    self._log("-> Sample: datasettet er tomt.")
                    return
                rng = np.random.default_rng(args['seed'])
                n_total = len(df_src)
                if 'count' in args:
                    n_keep = min(args['count'], n_total)
                    idx = rng.choice(df_src.index, size=n_keep, replace=False)
                else:
                    n_keep = max(1, int(n_total * args['fraction']))
                    idx = rng.choice(df_src.index, size=n_keep, replace=False)
                self.datasets[self.active_name] = df_src.loc[idx].reset_index(drop=True)
                self._log(f"-> Sample: beholdt {n_keep} av {n_total} observasjoner (seed={args['seed']}).")
                return

            # 2b. If-maskering: bare for kommandoer som bruker cond som delmengde (ikke for drop/keep/replace som bruker full df)
            _cond_filter_commands = frozenset([
                'sample', 'summarize', 'summarize-panel', 'tabulate', 'tabulate-panel', 'transitions-panel',
                'correlate', 'ci', 'anova', 'normaltest', 'collapse'
            ])
            if cond and cmd != 'generate' and cmd in _cond_filter_commands:
                mask = self._eval_condition_mask(df_target, cond)
                if mask is not None:
                    df_target = df_target.loc[mask].copy()
                else:
                    try:
                        df_target = df_target.loc[_py_eval_cond(df_target, cond)].copy()
                    except Exception:
                        df_target = df_target.query(cond).copy()
            if cond and cmd == 'generate':
                opts = dict(opts)
                opts['_condition'] = cond

            # 3. Data import
            if cmd in ['import', 'import-event', 'import-panel']:
                # Entitetstype-sjekk: variabler med ulik enhetstype kan ikke importeres i samme datasett
                _vpath = args.get('var', '') if isinstance(args, dict) else ''
                _vshort = _vpath.split('/')[-1] if _vpath else ''
                _vmeta = self.data_engine.catalog.get(_vshort) or self.data_engine.catalog.get(_vpath) or {}
                _var_entity = _vmeta.get('entity_type', 'person')
                _ds_entity  = self.dataset_entity_types.get(self.active_name)
                if _ds_entity is not None and _var_entity != _ds_entity:
                    _ds_disp  = _ENTITY_DISPLAY.get(_ds_entity, _ds_entity)
                    _var_disp = _ENTITY_DISPLAY.get(_var_entity, _var_entity)
                    self._log(
                        f"FEIL: Kan ikke importere «{_vshort}» (enhetstype: {_var_disp}) "
                        f"inn i et datasett av typen {_ds_disp}.\n"
                        f"Variabler med ulik enhetstype må ligge i separate datasett og "
                        f"kombineres via collapse og merge."
                    )
                    return
                # Oppdater datasett-entitetstype ved første import
                if _ds_entity is None and _vshort:
                    self.dataset_entity_types[self.active_name] = _var_entity

                new_data = self.data_engine.generate(cmd, args, df_target)
                # Omdøp unit_id → enhetstype-korrekt nøkkelkolonne (f.eks. PERSONID_1 for persondata)
                _id_col = _ENTITY_ID_COL.get(_var_entity, 'unit_id')
                if _id_col != 'unit_id' and 'unit_id' in new_data.columns:
                    new_data = new_data.rename(columns={'unit_id': _id_col})
                if df_target.empty and len(df_target.columns) <= 1:
                    # Helt tomt datasett (ingen kolonner utenom evt. nøkkel) — fyll direkte
                    self.datasets[self.active_name] = new_data
                else:
                    how = 'outer' if opts.get('outer_join') else 'left'
                    # NPR-datasett: bruk AGGRSHOPPID (unik per episode) som merge-nøkkel
                    _merge_key = (
                        'AGGRSHOPPID'
                        if self.dataset_entity_types.get(self.active_name) == _NPR_ENTITY
                        and 'AGGRSHOPPID' in df_target.columns
                        and 'AGGRSHOPPID' in new_data.columns
                        else _id_col
                    )
                    # Sørg for at merge-nøkkelen har samme dtype på begge sider (Pyodide int32/int64-problem)
                    if (_merge_key in df_target.columns and _merge_key in new_data.columns
                            and df_target[_merge_key].dtype != new_data[_merge_key].dtype):
                        new_data = new_data.copy()
                        new_data[_merge_key] = new_data[_merge_key].astype(df_target[_merge_key].dtype)
                    self.datasets[self.active_name] = pd.merge(df_target, new_data, on=_merge_key, how=how)

                # Registrer alias for variabelsti(er)
                if cmd in ['import', 'import-event']:
                    var_path = args.get('var', '')
                    alias = args.get('alias') or (var_path.split('/')[-1] if var_path else '')
                    self.label_manager.register_var_alias(alias, var_path)
                elif cmd == 'import-panel' and args.get('vars'):
                    for var_path in args['vars']:
                        short = var_path.split('/')[-1] if var_path else ''
                        self.label_manager.register_var_alias(short, var_path)

                # Etter lazy external_metadata: synkroniser kodelister til tabulate/list-labels
                self.label_manager.refresh_after_catalog_mutation()

                # Bygg microdata-lignende statuslinje
                df_after = self.datasets[self.active_name]
                n = len(df_after)
                try:
                    n_str = f"{n:,}".replace(",", " ")
                except Exception:
                    n_str = str(n)

                if cmd == 'import-panel':
                    var_list = args.get('vars') or []
                    short_names = [v.split('/')[-1] if '/' in v else v for v in var_list]
                    var_desc = ", ".join(short_names) if short_names else '?'
                    msg = f"Importerte {var_desc} som paneldata til {self.active_name} med {n_str} enheter"
                else:
                    var_path = args.get('var', '')
                    short = var_path.split('/')[-1] if var_path else var_path or '?'
                    alias = args.get('alias') or short
                    # Finn missing-verdier i alias-kolonnen hvis den finnes
                    missing = None
                    if alias in df_after.columns:
                        try:
                            missing = int(df_after[alias].isna().sum())
                        except Exception:
                            missing = None
                    date1 = args.get('date1')
                    date2 = args.get('date2')
                    if date1 and date2:
                        base = f"Importerte {short} i perioden {date1} til {date2} som {alias} til {self.active_name} med {n_str} enheter"
                    elif date1:
                        base = f"Importerte {short} på datoen {date1} som {alias} til {self.active_name} med {n_str} enheter"
                    else:
                        base = f"Importerte {short} som {alias} til {self.active_name} med {n_str} enheter"
                    if missing is not None and missing > 0:
                        try:
                            miss_str = f"{missing:,}".replace(",", " ")
                        except Exception:
                            miss_str = str(missing)
                        base += f", hvorav {miss_str} missingverdier"
                    msg = base

                self._log(msg)
                return

            # 4. Statistikk og Transformasjon
            run_opts = dict(opts)
            if cond and cmd == 'generate':
                run_opts['_condition'] = cond
            if cmd in ['tabulate', 'tabulate-panel', 'transitions-panel']:
                run_opts['_label_manager'] = self.label_manager
            if cmd in ['generate', 'aggregate', 'collapse', 'summarize', 'summarize-panel', 'correlate', 'ci', 'anova', 'tabulate', 'tabulate-panel', 'normaltest', 'transitions-panel']:
                # Egen logging for generate / collapse, mer microdata-lignende
                if cmd == 'generate':
                    result = self.stats_engine.execute(cmd, df_target, args, run_opts)
                    df_after = self.datasets[self.active_name]
                    target = args.get('target')
                    if target and target in df_after.columns:
                        n = len(df_after)
                        try:
                            n_str = f"{n:,}".replace(",", " ")
                        except Exception:
                            n_str = str(n)
                        missing = None
                        try:
                            missing = int(df_after[target].isna().sum())
                        except Exception:
                            missing = None
                        msg = f"Genererte {target} med {n_str} enheter"
                        if missing is not None and missing > 0:
                            try:
                                miss_str = f"{missing:,}".replace(",", " ")
                            except Exception:
                                miss_str = str(missing)
                            msg += f", hvorav {miss_str} missingverdier"
                        self._log(msg)
                    return

                result = self.stats_engine.execute(cmd, df_target, args, run_opts)
                if cmd == 'collapse':
                    # Collapse endrer datasettet radikalt
                    by_var = run_opts.get('by')
                    if by_var:
                        self.dataset_key_cols[self.active_name] = by_var
                    before_n = len(df_target)
                    self.datasets[self.active_name] = result
                    after_n = len(result) if hasattr(result, "__len__") else before_n
                    try:
                        after_str = f"{after_n:,}".replace(",", " ")
                    except Exception:
                        after_str = str(after_n)
                    if by_var:
                        self._log(f"Aggregerte {self.active_name} gruppert på {by_var} til {after_str} verdier")
                    else:
                        self._log(f"Aggregerte {self.active_name} til {after_str} verdier")
                elif result is not None:
                    header = f"\n--- Output: {cmd} ---\n"
                    if isinstance(result, (pd.DataFrame, pd.Series)):
                        # Konverter float-verdier i indeks/kolonner til int der mulig
                        def _intify_index(idx):
                            def _try_int(v):
                                try:
                                    fv = float(v)
                                    if pd.notna(fv) and fv == int(fv) and not isinstance(v, str):
                                        return int(fv)
                                except (TypeError, ValueError, OverflowError):
                                    pass
                                return v
                            try:
                                return pd.Index([_try_int(v) for v in idx])
                            except Exception:
                                return idx
                        if isinstance(result, pd.Series):
                            result = result.copy()
                            result.index = _intify_index(result.index)
                        elif isinstance(result, pd.DataFrame):
                            result = result.copy()
                            result.index = _intify_index(result.index)
                            result.columns = _intify_index(result.columns)
                        # to_html() viser alltid alle kolonner uten linjebryting
                        if isinstance(result, pd.Series):
                            html = result.to_frame('').to_html(border=0, classes='output-table',
                                                               max_rows=None, max_cols=None,
                                                               header=False)
                        else:
                            html = result.to_html(border=0, classes='output-table',
                                                  max_rows=None, max_cols=None)
                        # Legg til variabelnavn som data-attributter for tabulate
                        if cmd in ('tabulate', 'tabulate-panel') and isinstance(args, (list, tuple)):
                            v1 = args[0] if len(args) > 0 else ''
                            v2 = args[1] if len(args) > 1 else ''
                            html = html.replace(
                                'class="dataframe output-table"',
                                f'class="dataframe output-table" data-var1="{v1}" data-var2="{v2}"',
                                1)
                        self._log_embed('tablehtml', html)
                    else:
                        out_str = str(result)
                        lines = out_str.splitlines()
                        if lines:
                            last = lines[-1]
                            if ('dtype:' in last and last.lstrip().startswith('Name:')) or last.lstrip().startswith('Length:'):
                                lines = lines[:-1]
                        out_str = '\n'.join(lines)
                        self._log(f"{header}{out_str}\n")
                return

            # 4b. Figurkommandoer (barchart, histogram, boxplot, scatter, piechart, hexbin, sankey) – embed i output uten ekstra tekst
            if cmd in ['barchart', 'histogram', 'boxplot', 'scatter', 'piechart', 'hexbin', 'sankey']:
                plot_opts = dict(opts)
                plot_opts['_label_manager'] = self.label_manager
                fig = self.plot_handler.execute(cmd, df_target, args, plot_opts)
                if fig is not None:
                    import plotly.io as pio
                    self._log_embed('figure', pio.to_json(fig))
                else:
                    self._log(f"  FEIL PÅ KOMMANDO '{cmd}': Kunne ikke generere figur.")
                return

            # 5. Overlevelsesanalyse (cox, kaplan-meier, weibull)
            if cmd in ['cox', 'kaplan-meier', 'kaplan_meier', 'weibull']:
                surv_opts = dict(opts)
                surv_opts['_label_manager'] = self.label_manager
                result = self.survival_handler.execute(cmd, df_target, args, surv_opts)
                if isinstance(result, tuple):
                    summary, fig = result
                    if isinstance(summary, pd.DataFrame):
                        html = summary.to_html(border=0, classes='output-table',
                                               max_rows=None, max_cols=None)
                        self._log_embed('tablehtml', html)
                    else:
                        self._log(summary)
                    if fig is not None:
                        import plotly.io as pio
                        self._log_embed('figure', pio.to_json(fig))
                else:
                    self._log("  " + str(result))
                return

            # 6a. coefplot
            if cmd == 'coefplot':
                import plotly.graph_objects as go
                import plotly.io as pio
                reg_cmd = args.get('reg_cmd', 'regress') if isinstance(args, dict) else 'regress'
                reg_vars = args.get('vars', []) if isinstance(args, dict) else list(args)
                if not reg_vars:
                    self._log("  FEIL: coefplot krever avhengig variabel og minst én uavhengig variabel.")
                    return
                model, dep_var, indep_vars, df_clean = self.reg_engine._fit_simple(
                    reg_cmd, df_target, reg_vars, opts
                )
                params = model.params.drop('const', errors='ignore')
                try:
                    ci = model.conf_int().drop('const', errors='ignore')
                    lo = ci.iloc[:, 0].values.tolist()
                    hi = ci.iloc[:, 1].values.tolist()
                    err_minus = [c - l for c, l in zip(params.values.tolist(), lo)]
                    err_plus  = [h - c for c, h in zip(params.values.tolist(), hi)]
                except Exception:
                    err_minus = err_plus = None

                names = list(params.index)
                coefs = params.values.tolist()
                fig = go.Figure()
                fig.add_trace(go.Scatter(
                    x=coefs,
                    y=names,
                    mode='markers',
                    marker=dict(size=9, color='#2563eb'),
                    error_x=dict(
                        type='data', symmetric=False,
                        array=err_plus, arrayminus=err_minus,
                        thickness=1.5, width=6,
                    ) if err_minus is not None else None,
                ))
                fig.add_vline(x=0, line_dash='dot', line_color='#9ca3af', line_width=1)
                std_label = " (standardisert)" if opts.get('standardize') else ""
                fig.update_layout(
                    template='plotly_white',
                    margin=dict(l=50, r=50, t=40, b=60),
                    xaxis_title=f"Koeffisient{std_label}",
                    yaxis_title="Variabel",
                    yaxis=dict(autorange='reversed'),
                    height=max(300, len(names) * 45 + 120),
                )
                self._log_embed('figure', pio.to_json(fig))
                return

            # 6. Regresjon
            if cmd in ['regress', 'logit', 'probit', 'poisson', 'regress-panel', 'regress-panel-predict', 'regress-panel-diff', 'hausman', 'regress-predict', 'probit-predict', 'logit-predict', 'mlogit', 'mlogit-predict', 'ivregress', 'ivregress-predict', 'rdd']:
                result = self.reg_engine.execute(cmd, df_target, args, opts)
                summary, extra = result if isinstance(result, tuple) else (result, None)
                self._log(f"\n--- Modell: {cmd} ---\n{summary}\n")
                if extra:
                    for col_name, series in extra.items():
                        self.datasets[self.active_name][col_name] = series
                    self._log(f"  -> Lagt til variabler: {list(extra.keys())}")
                return

        except Exception as e:
            self._log(f"  FEIL PÅ KOMMANDO '{cmd}': {str(e)}")

    def _log(self, msg, indent=True):
        # Microdata-lignende: forklaringer/kommentarer under kommandoen innrykket med to mellomrom.
        # Kommandolinjen (dataset >> kommando) skal ikke ha innrykk; andre meldinger skal.
        if not indent:
            self.output_log.append(msg)
            return
        if isinstance(msg, str) and msg and not msg.startswith(("\n", " ")):
            self.output_log.append("  " + msg)
        else:
            self.output_log.append(msg)

    def _log_embed(self, embed_type, payload):
        """Embedd objekt i output: __micro_transform_start_<type>__ ... __micro_transform_end__"""
        start = MICRO_EMBED_START.format(embed_type)
        self.output_log.append(f"\n{start}\n{payload}\n{MICRO_EMBED_END}\n")
