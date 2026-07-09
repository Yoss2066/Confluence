// Minimal service worker — exists purely so this page can call
// registration.showNotification(), which is required on Android Chrome
// (the plain `new Notification()` constructor is blocked there).
self.addEventListener('install', (e) => {
  self.skipWaiting();
});
self.addEventListener('activate', (e) => {
  e.waitUntil(self.clients.claim());
});
