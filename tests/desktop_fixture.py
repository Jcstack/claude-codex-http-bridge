"""Create and own one ordinary GUI window. No automation test bridge/hooks."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

SWIFT = r'''
import AppKit
final class Fixture: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    var field: NSTextField!
    var count = 0
    let output = CommandLine.arguments[1]
    func applicationDidFinishLaunching(_ notification: Notification) {
        window = NSWindow(contentRect: NSRect(x: 80, y: 100, width: 440, height: 220), styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = "Bridge isolated fixture"
        window.isReleasedWhenClosed = false
        field = NSTextField(string: "INITIAL_FIXTURE")
        field.frame = NSRect(x: 25, y: 135, width: 390, height: 30)
        field.setAccessibilityIdentifier("bridge-fixture-input")
        field.setAccessibilityLabel("Fixture input")
        window.contentView!.addSubview(field)
        let button = NSButton(title: "Save fixture", target: self, action: #selector(save))
        button.frame = NSRect(x: 25, y: 65, width: 160, height: 40)
        button.setAccessibilityIdentifier("bridge-fixture-save")
        window.contentView!.addSubview(button)
        window.orderFrontRegardless()
        write()
    }
    @objc func save() { count += 1; write() }
    func write() {
        let value: [String: Any] = ["id": window.windowNumber, "pid": ProcessInfo.processInfo.processIdentifier, "value": field.stringValue, "count": count]
        if let data = try? JSONSerialization.data(withJSONObject: value) { try? data.write(to: URL(fileURLWithPath: output), options: .atomic) }
    }
}
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = Fixture()
app.delegate = delegate
app.run()
'''

CSHARP = r'''
using System;
using System.IO;
using System.Diagnostics;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Windows.Forms;
[DataContract] class State {
  [DataMember] public long id;
  [DataMember] public int pid;
  [DataMember] public string value;
  [DataMember] public int count;
}
class Fixture : Form {
  TextBox input = new TextBox(); Button save = new Button(); string path; int count;
  public Fixture(string output) {
    path=output; Text="Bridge isolated fixture"; Width=440; Height=250;
    input.Text="INITIAL_FIXTURE"; input.AccessibleName="Fixture input"; input.SetBounds(25,25,360,30);
    save.Text="Save fixture"; save.AccessibleName="Save fixture"; save.SetBounds(25,100,160,40);
    Controls.Add(input); Controls.Add(save);
    save.Click += delegate { count++; Write(); }; Shown += delegate { Write(); };
  }
  void Write() {
    var state = new State { id=Handle.ToInt64(), pid=Process.GetCurrentProcess().Id, value=input.Text, count=count };
    using (var data=new MemoryStream()) {
      new DataContractJsonSerializer(typeof(State)).WriteObject(data,state);
      File.WriteAllBytes(path,data.ToArray());
    }
  }
  [STAThread] static void Main(string[] args) { Application.Run(new Fixture(args[0])); }
}
'''


@contextmanager
def fixture():
    with tempfile.TemporaryDirectory(prefix="bridge-desktop-fixture-") as temporary:
        root = Path(temporary); state = root / "state.json"
        if sys.platform == "darwin":
            source = root / "Fixture.swift"; source.write_text(SWIFT)
            binary = root / "BridgeDesktopFixture"
            build = subprocess.run(["/usr/bin/swiftc", str(source), "-o", str(binary)], capture_output=True, timeout=120)
        elif os.name == "nt":
            source = root / "Fixture.cs"; source.write_text(CSHARP)
            binary = root / "BridgeDesktopFixture.exe"
            compiler = Path(os.environ["SYSTEMROOT"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
            build = subprocess.run([str(compiler), "/nologo", "/target:winexe", "/reference:System.Windows.Forms.dll",
                "/reference:System.Runtime.Serialization.dll", "/out:"+str(binary), str(source)], capture_output=True, timeout=120)
        else: raise RuntimeError("The scoped GUI fixture requires macOS or Windows")
        if build.returncode: raise RuntimeError("Owned desktop fixture compilation failed")
        process = subprocess.Popen([str(binary), str(state)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic()+30
            while time.monotonic() < deadline:
                if process.poll() is not None: raise RuntimeError("Owned desktop fixture exited early")
                if state.exists():
                    try:
                        value = json.loads(state.read_text())
                        if value.get("pid") == process.pid: break
                    except (OSError, ValueError): pass
                time.sleep(0.1)
            else: raise RuntimeError("Owned desktop fixture never became ready")
            yield process, state, value
        finally:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
