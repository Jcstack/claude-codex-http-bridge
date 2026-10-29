"""Lock Windows runtime data to the current user with native NTFS ACLs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


_LOCK = r"""
$ErrorActionPreference = 'Stop'
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
foreach ($path in @(ConvertFrom-Json $env:CLAUDE_CODEX_ACL_PATHS)) {
    $item = Get-Item -LiteralPath $path -Force
    $acl = Get-Acl -LiteralPath $path
    if ($item.PSIsContainer) {
        $sddl = "D:P(A;OICI;FA;;;$sid)"
    } else {
        $sddl = "D:P(A;;FA;;;$sid)"
    }
    $acl.SetSecurityDescriptorSddlForm(
        $sddl, [Security.AccessControl.AccessControlSections]::Access
    )
    Set-Acl -LiteralPath $path -AclObject $acl
}
"""

_VERIFY = r"""
$ErrorActionPreference = 'Stop'
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$protected = $env:CLAUDE_CODEX_REQUIRE_PROTECTED -eq '1'
foreach ($path in @(ConvertFrom-Json $env:CLAUDE_CODEX_ACL_PATHS)) {
    $acl = Get-Acl -LiteralPath $path
    if ($protected -and -not $acl.AreAccessRulesProtected) {
        throw 'Windows runtime ACL still inherits permissions'
    }
    $rules = @($acl.GetAccessRules(
        $true, $true, [Security.Principal.SecurityIdentifier]
    ))
    if ($rules.Count -eq 0) {
        throw 'Windows runtime ACL has no user access rule'
    }
    foreach ($rule in $rules) {
        if ($rule.IdentityReference.Value -ne $sid -or
            $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) {
            throw 'Windows runtime ACL grants another identity access'
        }
    }
}
"""


def _powershell(script: str, paths: list[Path], protected: bool = False) -> None:
    if not paths:
        return
    environment = os.environ.copy()
    environment["CLAUDE_CODEX_ACL_PATHS"] = json.dumps([str(path) for path in paths])
    environment["CLAUDE_CODEX_REQUIRE_PROTECTED"] = "1" if protected else "0"
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            env=environment,
            capture_output=True,
            check=True,
            timeout=45,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("Could not apply or verify private Windows ACLs (NTFS required)") from error


def lock(paths: list[Path]) -> None:
    _powershell(_LOCK, paths)
    verify(paths, protected=True)


def verify(paths: list[Path], protected: bool = False) -> None:
    _powershell(_VERIFY, paths, protected)
