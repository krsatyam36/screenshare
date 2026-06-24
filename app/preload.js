const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('api', {
  startServer:  () => ipcRenderer.invoke('server:start'),
  stopServer:   () => ipcRenderer.invoke('server:stop'),
  serverInfo:   () => ipcRenderer.invoke('server:info'),
  getSettings:  () => ipcRenderer.invoke('settings:get'),
  setSettings:  (p) => ipcRenderer.invoke('settings:set', p),
  listHistory:  () => ipcRenderer.invoke('history:list'),
  getSession:   (id) => ipcRenderer.invoke('history:get', id),
  meta:         () => ipcRenderer.invoke('history:meta'),
  checkUpdates: () => ipcRenderer.invoke('updates:check'),
  openExternal: (u) => ipcRenderer.invoke('app:openExternal', u),
  qr:           (u) => ipcRenderer.invoke('qr:generate', u),
  appVersion:   () => ipcRenderer.invoke('app:version'),
  closeChoice:  (c) => ipcRenderer.send('app:close-choice', c),
  on: (channel, cb) => {
    const allowed = ['log', 'state', 'status', 'url', 'confirm-close'];
    if (allowed.includes(channel)) ipcRenderer.on(channel, (_e, ...a) => cb(...a));
  },
});
