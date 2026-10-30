"""Native window identity checks. No screenshot, activation, input or network."""

from __future__ import annotations

import ctypes as C
import asyncio
import os
import plistlib
import sys


class ScopeError(RuntimeError):
    pass


def _blocked(path: str) -> bool:
    name = path.lower().replace(" ", "")
    return any(value in name for value in ("1password", "bitwarden", "keepass", "dashlane", "lastpass", "protonpass",
        "nordpass", "securityagent", "systemsettings", "systempreferences", "windowsterminal", "iterm", "/terminal.app/",
        "powershell.exe", "pwsh.exe", "cmd.exe", "credentialui", "logonui"))


class MacWindows:
    def __init__(self):
        self.cf = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.cg = C.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        self.ax = C.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        self.proc = C.CDLL("/usr/lib/libproc.dylib")
        for lib, name, result, args in (
            (self.cg, "CGWindowListCopyWindowInfo", C.c_void_p, [C.c_uint32, C.c_uint32]),
            (self.cg, "CGEventCreateKeyboardEvent", C.c_void_p, [C.c_void_p, C.c_uint16, C.c_bool]),
            (self.cg, "CGEventKeyboardSetUnicodeString", None, [C.c_void_p, C.c_ulong, C.POINTER(C.c_uint16)]),
            (self.cg, "CGEventSetFlags", None, [C.c_void_p, C.c_uint64]),
            (self.cg, "CGEventPostToPid", None, [C.c_int, C.c_void_p]),
            (self.cf, "CFPropertyListCreateData", C.c_void_p, [C.c_void_p, C.c_void_p, C.c_long, C.c_ulong, C.c_void_p]),
            (self.cf, "CFDataGetLength", C.c_long, [C.c_void_p]),
            (self.cf, "CFDataGetBytePtr", C.c_void_p, [C.c_void_p]),
            (self.cf, "CFRelease", None, [C.c_void_p]),
            (self.cf, "CFStringCreateWithCString", C.c_void_p, [C.c_void_p, C.c_char_p, C.c_uint32]),
            (self.cf, "CFArrayGetCount", C.c_long, [C.c_void_p]),
            (self.cf, "CFArrayGetValueAtIndex", C.c_void_p, [C.c_void_p, C.c_long]),
            (self.cf, "CFBooleanGetValue", C.c_bool, [C.c_void_p]),
            (self.cf, "CFStringGetCString", C.c_bool, [C.c_void_p, C.c_void_p, C.c_long, C.c_uint32]),
            (self.cf, "CFStringGetLength", C.c_long, [C.c_void_p]),
            (self.ax, "AXUIElementCreateApplication", C.c_void_p, [C.c_int]),
            (self.ax, "AXUIElementCopyAttributeValue", C.c_int, [C.c_void_p, C.c_void_p, C.POINTER(C.c_void_p)]),
            (self.ax, "_AXUIElementGetWindow", C.c_int, [C.c_void_p, C.POINTER(C.c_uint32)]),
            (self.ax, "AXValueCreate", C.c_void_p, [C.c_int, C.c_void_p]),
            (self.ax, "AXUIElementSetAttributeValue", C.c_int, [C.c_void_p, C.c_void_p, C.c_void_p]),
            (self.proc, "proc_pidinfo", C.c_int, [C.c_int, C.c_int, C.c_uint64, C.c_void_p, C.c_int]),
            (self.proc, "proc_pidpath", C.c_int, [C.c_int, C.c_void_p, C.c_uint32]),
        ):
            function = getattr(lib, name); function.restype = result; function.argtypes = args

    def windows(self) -> list[dict]:
        array = self.cg.CGWindowListCopyWindowInfo(1 | 16, 0)  # on-screen, exclude desktop
        if not array: raise ScopeError("Visible window metadata is unavailable")
        data = None
        try:
            data = self.cf.CFPropertyListCreateData(None, array, 200, 0, None)
            if not data: raise ScopeError("Window metadata could not be decoded")
            items = plistlib.loads(C.string_at(self.cf.CFDataGetBytePtr(data), self.cf.CFDataGetLength(data)))
            return [{"id": item["kCGWindowNumber"], "pid": item["kCGWindowOwnerPID"],
                     "app": item.get("kCGWindowOwnerName", ""), "title": item.get("kCGWindowName", "")}
                    for item in items if item.get("kCGWindowLayer") == 0 and item.get("kCGWindowIsOnscreen")
                    and item.get("kCGWindowBounds", {}).get("Width", 0) > 0
                    and item.get("kCGWindowBounds", {}).get("Height", 0) > 0]
        finally:
            if data: self.cf.CFRelease(data)
            self.cf.CFRelease(array)

    def attribute(self, element, name):
        key = self.cf.CFStringCreateWithCString(None, name.encode(), 0x08000100)
        output = C.c_void_p()
        try:
            if self.ax.AXUIElementCopyAttributeValue(element, key, C.byref(output)):
                raise ScopeError("Accessibility cannot confirm the selected window; no action was sent")
            return output.value
        finally: self.cf.CFRelease(key)

    def window_number(self, element) -> int:
        number = C.c_uint32()
        if self.ax._AXUIElementGetWindow(element, C.byref(number)):
            raise ScopeError("Accessibility window identity is unavailable")
        return number.value

    def identity(self, window_id: int, *, keyboard: bool = False) -> dict:
        item = next((item for item in self.windows() if item["id"] == window_id), None)
        if item is None: raise ScopeError("The selected window is closed, hidden or minimized; select a visible window again")
        pid = item["pid"]
        # proc_bsdinfo ends with two uint64 start-time fields (seconds/usec).
        info = C.create_string_buffer(136)
        if self.proc.proc_pidinfo(pid, 3, 0, info, len(info)) != len(info):
            raise ScopeError("The selected process identity is unavailable")
        created = info.raw[120:136].hex()
        path = C.create_string_buffer(4096)
        if self.proc.proc_pidpath(pid, path, len(path)) <= 0:
            raise ScopeError("The selected executable identity is unavailable")
        executable = os.fsdecode(path.value)
        if _blocked(executable): raise ScopeError("This sensitive or terminal application is outside desktop scope")
        app = self.ax.AXUIElementCreateApplication(pid)
        windows, focused = None, None
        try:
            windows = self.attribute(app, "AXWindows")
            matches = []
            for index in range(self.cf.CFArrayGetCount(windows)):
                element = self.cf.CFArrayGetValueAtIndex(windows, index)
                try:
                    if self.window_number(element) == window_id: matches.append(element)
                except ScopeError: continue
            if len(matches) != 1: raise ScopeError("Accessibility did not confirm one live selected window")
            minimized = self.attribute(matches[0], "AXMinimized")
            try:
                if self.cf.CFBooleanGetValue(minimized): raise ScopeError("Minimized windows are outside desktop scope")
            finally: self.cf.CFRelease(minimized)
            if keyboard:
                focused = self.attribute(app, "AXFocusedWindow")
                if self.window_number(focused) != window_id:
                    raise ScopeError("Keyboard input requires the selected window to own this application's keyboard focus")
        finally:
            if focused: self.cf.CFRelease(focused)
            if windows: self.cf.CFRelease(windows)
            if app: self.cf.CFRelease(app)
        return {"id": window_id, "pid": pid, "created": created, "executable": executable}

    def keyboard_target(self, app, window_id, *, keep_element=False):
        focused, element, owner, role = None, None, None, None
        try:
            focused = self.attribute(app, "AXFocusedWindow")
            if self.window_number(focused) != window_id: raise ScopeError("The selected window lost keyboard focus")
            element = self.attribute(app, "AXFocusedUIElement")
            owner = self.attribute(element, "AXWindow")
            if self.window_number(owner) != window_id: raise ScopeError("The focused element belongs to a different window")
            role = self.attribute(element, "AXRole")
            buffer = C.create_string_buffer(128)
            if not self.cf.CFStringGetCString(role, buffer, len(buffer), 0x08000100) or buffer.value not in (b"AXTextField", b"AXTextArea", b"AXComboBox"):
                raise ScopeError("Literal text requires a focused editable, non-password element in the selected window")
            if keep_element:
                result, element = element, None
                return result
        finally:
            for value in (focused, element, owner, role):
                if value: self.cf.CFRelease(value)

    def select_all_text(self, window_id):
        identity = self.identity(window_id, keyboard=True)
        app = self.ax.AXUIElementCreateApplication(identity["pid"])
        element, value, selected, key = None, None, None, None
        try:
            element = self.keyboard_target(app, window_id, keep_element=True)
            value = self.attribute(element, "AXValue")
            # Selection is scoped to the actual focused text element. Command-A
            # as an app event can otherwise depend on the foreground menu bar.
            class Range(C.Structure): _fields_ = [("location", C.c_long), ("length", C.c_long)]
            span = Range(0, self.cf.CFStringGetLength(value))
            selected = self.ax.AXValueCreate(4, C.byref(span))  # kAXValueCFRangeType
            key = self.cf.CFStringCreateWithCString(None, b"AXSelectedTextRange", 0x08000100)
            if not selected or self.ax.AXUIElementSetAttributeValue(element, key, selected):
                raise ScopeError("The selected text element did not confirm select-all; no keyboard fallback was attempted")
        finally:
            for handle in (element, value, selected, key, app):
                if handle: self.cf.CFRelease(handle)

    async def type_text(self, window_id: int, text: str):
        identity = self.identity(window_id, keyboard=True)
        # Encode before sending anything. Split by Unicode characters, keeping
        # UTF-16 surrogate pairs together in the OS event's short text payload.
        chunks, chunk = [], b""
        for character in text:
            encoded = character.encode("utf-16-le")
            if len(chunk)+len(encoded) > 40: chunks.append(chunk); chunk = b""
            chunk += encoded
        if chunk: chunks.append(chunk)
        app = self.ax.AXUIElementCreateApplication(identity["pid"])
        try:
            for raw in chunks:
                self.keyboard_target(app, window_id)
                data = (C.c_uint16*(len(raw)//2)).from_buffer_copy(raw)
                down = self.cg.CGEventCreateKeyboardEvent(None, 0, True)
                up = self.cg.CGEventCreateKeyboardEvent(None, 0, False)
                try:
                    if not down or not up: raise ScopeError("Targeted keyboard events are unavailable; no alternative input method was attempted")
                    for event in (down, up):
                        self.cg.CGEventSetFlags(event, 0)
                        self.cg.CGEventKeyboardSetUnicodeString(event, len(data), data)
                        self.cg.CGEventPostToPid(identity["pid"], event)
                finally:
                    if down: self.cf.CFRelease(down)
                    if up: self.cf.CFRelease(up)
                await asyncio.sleep(0)  # Cancellation stops subsequent chunks.
        finally:
            if app: self.cf.CFRelease(app)


class WindowsWindows:
    def __init__(self):
        from ctypes import wintypes as W
        self.W = W
        self.user = C.WinDLL("user32", use_last_error=True)
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        for lib, name, result, args in (
            (self.user, "IsWindowVisible", W.BOOL, [W.HWND]), (self.user, "IsIconic", W.BOOL, [W.HWND]),
            (self.user, "GetWindowThreadProcessId", W.DWORD, [W.HWND, C.POINTER(W.DWORD)]),
            (self.user, "GetWindowTextW", C.c_int, [W.HWND, W.LPWSTR, C.c_int]),
            (self.user, "GetForegroundWindow", W.HWND, []),
            (self.user, "GetWindowRect", W.BOOL, [W.HWND, C.POINTER(W.RECT)]),
            (self.user, "GetAncestor", W.HWND, [W.HWND, W.UINT]),
            (self.user, "GetClassNameW", C.c_int, [W.HWND, W.LPWSTR, C.c_int]),
            (self.user, "GetWindowLongPtrW", C.c_ssize_t, [W.HWND, C.c_int]),
            (self.user, "SendMessageTimeoutW", C.c_ssize_t, [W.HWND, W.UINT, W.WPARAM, W.LPARAM, W.UINT, W.UINT, C.POINTER(C.c_size_t)]),
            (self.kernel, "OpenProcess", W.HANDLE, [W.DWORD, W.BOOL, W.DWORD]),
            (self.kernel, "GetProcessTimes", W.BOOL, [W.HANDLE, C.POINTER(W.FILETIME), C.POINTER(W.FILETIME), C.POINTER(W.FILETIME), C.POINTER(W.FILETIME)]),
            (self.kernel, "QueryFullProcessImageNameW", W.BOOL, [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]),
            (self.kernel, "CloseHandle", W.BOOL, [W.HANDLE]),
        ):
            function = getattr(lib, name); function.restype = result; function.argtypes = args

    def bounds(self, window_id):
        # Keep the bridge thread's prior DPI mode. GetWindowRect otherwise
        # returns virtualized coordinates while the native capture is pixels.
        self.user.SetThreadDpiAwarenessContext.argtypes = [C.c_void_p]
        self.user.SetThreadDpiAwarenessContext.restype = C.c_void_p
        previous = self.user.SetThreadDpiAwarenessContext(C.c_void_p(-4))
        try:
            rect = self.W.RECT()
            if not self.user.GetWindowRect(window_id, C.byref(rect)): raise ScopeError("Window bounds are unavailable")
            return (rect.left, rect.top, rect.right, rect.bottom)
        finally:
            if previous: self.user.SetThreadDpiAwarenessContext(previous)

    def windows(self) -> list[dict]:
        result = []; W = self.W
        callback_type = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        @callback_type
        def callback(window, _data):
            if self.user.IsWindowVisible(window) and not self.user.IsIconic(window):
                pid = W.DWORD(); self.user.GetWindowThreadProcessId(window, C.byref(pid))
                title = C.create_unicode_buffer(4096); self.user.GetWindowTextW(window, title, len(title))
                result.append({"id": int(window), "pid": pid.value, "title": title.value})
            return True
        self.user.EnumWindows.argtypes = [callback_type, W.LPARAM]; self.user.EnumWindows.restype = W.BOOL
        if not self.user.EnumWindows(callback, 0): raise ScopeError("Visible window enumeration failed")
        return result

    def identity(self, window_id: int, *, keyboard: bool = False) -> dict:
        W = self.W
        if not self.user.IsWindowVisible(window_id) or self.user.IsIconic(window_id):
            raise ScopeError("The selected window is closed, hidden or minimized")
        pid = W.DWORD()
        if not self.user.GetWindowThreadProcessId(window_id, C.byref(pid)): raise ScopeError("Window process identity is unavailable")
        process = self.kernel.OpenProcess(0x1000, False, pid.value)
        if not process: raise ScopeError("The selected process is outside the current user's access")
        try:
            times = [W.FILETIME() for _ in range(4)]
            if not self.kernel.GetProcessTimes(process, *(C.byref(t) for t in times)): raise ScopeError("Process creation time is unavailable")
            path = C.create_unicode_buffer(32768); length = W.DWORD(len(path))
            if not self.kernel.QueryFullProcessImageNameW(process, 0, path, C.byref(length)): raise ScopeError("Executable identity is unavailable")
            if _blocked(path.value): raise ScopeError("This sensitive or terminal application is outside desktop scope")
            if keyboard and self.user.GetForegroundWindow() != window_id:
                raise ScopeError("Keyboard input requires the selected window to be foreground; the bridge does not activate it")
            return {"id": window_id, "pid": pid.value, "created": str(times[0].dwLowDateTime | times[0].dwHighDateTime << 32), "executable": path.value}
        finally: self.kernel.CloseHandle(process)

    def focused_edit(self, window_id):
        identity = self.identity(window_id, keyboard=True)
        W = self.W
        class GUIInfo(C.Structure):
            _fields_ = [("cbSize",W.DWORD),("flags",W.DWORD),("active",W.HWND),("focus",W.HWND),
                        ("capture",W.HWND),("menu_owner",W.HWND),("move_size",W.HWND),("caret",W.HWND),("rect",W.RECT)]
        self.user.GetGUIThreadInfo.argtypes = [W.DWORD,C.POINTER(GUIInfo)]
        self.user.GetGUIThreadInfo.restype = W.BOOL
        pid = W.DWORD(); thread = self.user.GetWindowThreadProcessId(window_id,C.byref(pid))
        info = GUIInfo(); info.cbSize = C.sizeof(info)
        if not self.user.GetGUIThreadInfo(thread,C.byref(info)) or not info.focus or self.user.GetAncestor(info.focus,2) != window_id:
            raise ScopeError("The selected window has no confirmed focused text control")
        self.user.GetWindowThreadProcessId(info.focus,C.byref(pid))
        name = C.create_unicode_buffer(256); self.user.GetClassNameW(info.focus,name,len(name))
        classname = name.value.lower()
        if pid.value != identity["pid"] or not (classname == "edit" or classname.startswith(("richedit","windowsforms10.edit"))):
            raise ScopeError("Text input/selection requires a focused native Edit/RichEdit control; no global fallback was attempted")
        if self.user.GetWindowLongPtrW(info.focus,-16) & (0x20 | 0x800):
            raise ScopeError("Password and read-only text controls are outside literal input scope")
        return info.focus

    def select_all_text(self, window_id):
        target = self.focused_edit(window_id)
        result = C.c_size_t()
        # EM_SETSEL has element-local semantics. Targeted WM_KEYDOWN Ctrl+A
        # does not reliably carry the control state to another UI thread.
        if not self.user.SendMessageTimeoutW(target,0xB1,0,-1,2,1000,C.byref(result)):
            raise ScopeError("Text selection was not confirmed; observe before continuing")

    async def type_text(self, window_id, text):
        target = self.focused_edit(window_id)
        value = C.create_unicode_buffer(text)
        result = C.c_size_t()
        # EM_REPLACESEL preserves the current selection. Upstream's helper
        # first moves to the end and can append to the first editable control
        # instead of the focused control; neither behavior is used here.
        pointer = C.cast(value,C.c_void_p).value
        if not self.user.SendMessageTimeoutW(target,0xC2,1,pointer,2,1000,C.byref(result)):
            raise ScopeError("Text insertion has an uncertain outcome; observe before continuing. No input was retried")


def inventory():
    if sys.platform == "darwin": return MacWindows()
    if os.name == "nt": return WindowsWindows()
    raise ScopeError("Scoped desktop support requires macOS or Windows; no whole-display Linux fallback is enabled")
