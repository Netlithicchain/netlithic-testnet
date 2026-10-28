# Run a Netlithic test node (easy way)

Test coins have no value. This only runs a node that keeps a copy of the test network.
No wallet or key is made, and nothing private leaves your computer.

## Windows
1. Install Python from python.org (tick "Add python.exe to PATH").
2. Download the Netlithic folder, right-click `install.ps1` and pick "Run with PowerShell".
3. When the window says "Done", the node is running and starts each time you log in. Open
   http://127.0.0.1:8000/status to see it. If something goes wrong the installer says so in plain words,
   shows what Windows answered, and does NOT say "Done".
   - **No administrator needed.** The installer first tries a Windows scheduled task ("Installed as a
     scheduled task"). Many normal accounts are not allowed to make one ("Access is denied"). Then it
     puts a small file, `Netlithic Node.cmd`, in your own Startup folder
     (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`) and says so: "I added the node to your
     Startup folder instead. It starts when you log in." Either way the node starts right away.
   - Both methods run a small file, `start-node.cmd`, in the Netlithic folder; it works even if your user
     folder name has spaces, like `C:\Users\Jane Doe`.
   - `python easy_node.py uninstall` removes both the task and the Startup file and stops the node. It
     tells you plainly if anything is left (for example a task made earlier by an administrator: run
     uninstall once as administrator).
   - Running the installer again is safe: it stops the old copy first and never leaves two launchers.

## Mac
1. Install Python from python.org.
2. Open Terminal in the folder and paste: `sh install.sh` (one line, one time).
3. Done. It runs in the background (a "launch agent") and starts at login.

## Linux
`sh install.sh` (you may need `sudo apt install python3-venv` first). It becomes a user
service: `systemctl --user status netlithic-node`. To keep it running after logout:
`loginctl enable-linger $USER`.

## Docker
`docker compose up -d`. Data stays in the `netlithic-data` volume. To let other nodes reach
you, change the port line to `"8000:8000"` and set `ADVERTISE_URL` to your public address.

## Where things live
- Windows: `%LOCALAPPDATA%\Netlithic` · Mac: `~/Library/Application Support/Netlithic` ·
  Linux: `~/.local/share/Netlithic` (a `venv` folder and a `node` folder with the chain).
- The node joins 137.184.199.238:8000 and 157.230.63.97:8000.

## Remove
`python easy_node.py uninstall` stops the node, waits a few seconds for Windows to release its files, then removes the service and the private Python folder. Your data folder is kept and never touched. If a file is still in use it says so in plain words (close the node, or log out and back in, then run uninstall again) and does not claim it finished.

## Deploy plan (for the team)
Nothing runs on the servers. Publish = put these files in the public download / repo; the
seeds just receive normal peer syncs. The Desktop app remains the no-terminal option for
most people; this is for people who want an always-on node. Mac/Windows service steps are
written but only Linux was tested on a real machine.
