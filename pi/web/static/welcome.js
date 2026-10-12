// The sign-in popup is a limited mini-browser, so this page only points the
// user at the real browser, where the full app (including uploads) works.
const address = document.getElementById('address').textContent;
const note = document.getElementById('note');
if (/Android/i.test(navigator.userAgent)) {
  // Chrome's intent URL leaves the sign-in popup and opens the full app.
  document.getElementById('open').href = 'intent://10.42.0.1/#Intent;scheme=http;package=com.android.chrome;end';
}
document.getElementById('copy').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(address);
    note.textContent = 'Copied. Paste it into your browser’s address bar.';
  } catch (error) {
    const range = document.createRange(); range.selectNodeContents(document.getElementById('address'));
    const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
    note.textContent = 'Address selected. Copy it, then paste it into your browser.';
  }
});

// Done: tell the device this phone has the address, then re-run the OS
// probe (which now returns "success") so the sign-in sheet closes itself.
document.getElementById('done').addEventListener('click', async () => {
  try {
    await fetch('/api/portal/done', {method: 'POST'});
    note.textContent = 'All set. Open your browser and go to ' + address;
    location.href = '/hotspot-detect.html';
  } catch (error) {
    note.textContent = 'Could not finish. Close this window and open ' + address + ' in your browser.';
  }
});

// Join Wi-Fi: drop the hotspot and try saved networks (e.g. Mitera or a phone
// hotspot) for 30 seconds; the hotspot comes back if none connects.
document.getElementById('join').addEventListener('click', async () => {
  if (!confirm('Stop the hotspot and try saved Wi-Fi for 30 seconds? You will be disconnected. If no network connects, the hotspot comes back.')) return;
  try {
    const response = await fetch('/api/wifi/search', {method: 'POST'});
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).error || 'request failed');
    note.textContent = 'Hotspot off. Trying saved Wi-Fi for 30 seconds… if none connects, the hotspot returns.';
  } catch (error) {
    note.textContent = 'Could not stop the hotspot: ' + error.message;
  }
});
