// Service worker of the Spanreed web app: shows push notifications and
// opens the app when one is tapped. It caches nothing; the app needs the
// server for everything it shows.
"use strict";

const APP_URL = "/app/";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

async function appWindows() {
  const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  return windows.filter((w) => new URL(w.url).pathname.startsWith(APP_URL));
}

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { /* not JSON */ }
  event.waitUntil((async () => {
    // The app is on screen: what the push announces is already shown there.
    // A test notification is shown anyway, since seeing it is the point.
    const visible = (await appWindows()).some((w) => w.visibilityState === "visible");
    if (visible && data.kind !== "test") return;
    const options = {
      body: data.body || "",
      icon: "/app/icons/icon-192.png",
      badge: "/app/icons/icon-192.png",
      data: { url: APP_URL },
      // A prompt stays on screen until it is answered or swiped away.
      requireInteraction: data.kind === "prompt",
    };
    if (data.tag) { options.tag = data.tag; options.renotify = true; }
    await self.registration.showNotification(data.title || "Spanreed", options);
  })());
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil((async () => {
    const windows = await appWindows();
    if (windows.length) return windows[0].focus();
    return self.clients.openWindow(APP_URL);
  })());
});
