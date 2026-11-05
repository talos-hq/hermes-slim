/* global workspace */
// An explicit launcher click brings the chats here before Electron requests focus.
// Activating first lets KWin switch to the window's previous desktop. Pop-outs share
// the main window's class and title, so they all move: Electron then focuses the main
// window, and one left behind would switch the desktop. Windows pinned to all desktops
// and taskbar-less helpers stay as they are.
const windows = workspace.windowList().filter(
  window => window.normalWindow && window.resourceClass === 'hermes' && !window.skipTaskbar
);
for (const window of windows) {
  if (!window.onAllDesktops) {
    window.desktops = [workspace.currentDesktop];
  }
}
if (windows.length > 0) {
  windows[0].minimized = false;
  workspace.activeWindow = windows[0];
}
