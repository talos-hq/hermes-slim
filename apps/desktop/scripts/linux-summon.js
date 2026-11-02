/* global workspace */
// An explicit launcher click brings the chat here before Electron requests focus.
// Activating first lets KWin switch to the window's previous desktop.
const windows = workspace.windowList();
for (const window of windows) {
  if (window.normalWindow && window.resourceClass === 'hermes') {
    window.desktops = [workspace.currentDesktop];
    window.minimized = false;
    workspace.activeWindow = window;
    break;
  }
}
