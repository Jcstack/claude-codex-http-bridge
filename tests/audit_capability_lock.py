"""Query OSV for this repository's public pins only; never inspect accounts."""

import json
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs): return None


def main():
    source = Path(__file__).resolve().parents[1]/"requirements-capabilities.lock"
    pairs = sorted(set(re.findall(r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+!-]+)", source.read_text(), re.M)))
    if not pairs: raise RuntimeError("No public dependency pins found")
    opener = urllib.request.build_opener(NoRedirect())
    findings = {pair: set() for pair in pairs}
    pending = [(pair, {"package": {"name": pair[0], "ecosystem": "PyPI"}, "version": pair[1]}) for pair in pairs]
    for _page in range(10):
        request = urllib.request.Request("https://api.osv.dev/v1/querybatch",
            data=json.dumps({"queries": [query for _pair,query in pending]}).encode(), headers={"Content-Type":"application/json"})
        with opener.open(request, timeout=30) as response: raw=response.read((8<<20)+1)
        if len(raw)>8<<20: raise RuntimeError("Advisory response exceeded the bound")
        results=json.loads(raw).get("results",[])
        if len(results)!=len(pending): raise RuntimeError("Incomplete advisory response")
        following=[]
        for (pair,query),result in zip(pending,results):
            for advisory in result.get("vulns",[]):
                identifier=advisory.get("id","")
                if not re.fullmatch(r"[A-Za-z0-9_-]{3,100}",identifier): raise RuntimeError("Invalid advisory identifier")
                findings[pair].add(identifier)
            token=result.get("next_page_token")
            if token:
                if not isinstance(token,str) or len(token)>8192: raise RuntimeError("Invalid advisory pagination")
                following.append((pair,{**query,"page_token":token}))
        pending=following
        if not pending: break
    if pending: raise RuntimeError("Advisory pagination did not complete")
    matches=[{"package":name,"version":version,"advisories":sorted(identifiers)}
             for (name,version),identifiers in findings.items() if identifiers]
    print(json.dumps({"database":"OSV","public_dependency_names_only":True,"packages_checked":len(pairs),
                      "complete":True,"matches":matches}))
    return 1 if matches else 0


if __name__=="__main__":
    try: raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"database":"OSV","complete":False,"category":type(error).__name__}),file=sys.stderr)
        raise SystemExit(2) from None
