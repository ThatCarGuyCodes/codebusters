# Put Decipher on GitHub Pages

GitHub Pages publishes the interface. A separate Python server is required for password protected rooms, PDF storage, and live answers. Both the Pages URL and server URL must use HTTPS for browsers to connect reliably over WSS.

## 1. Put the server online

Deploy this project to a Python or Docker host that supports WebSockets and a **persistent volume**. The included `Dockerfile` listens on the host's `PORT` or port 8000. Mount a volume at `/app/data`; it holds the PDFs and SQLite database. Run exactly one server process against that volume.

Set these server environment variables:

```text
CODEBUSTERS_DATA_DIR=/app/data
CODEBUSTERS_ALLOWED_ORIGINS=https://YOUR_GITHUB_USERNAME.github.io
```

Use the origin only for `CODEBUSTERS_ALLOWED_ORIGINS`: no `/repository/` path and no trailing slash. If you later add a custom domain, add its exact HTTPS origin to the comma separated list. Your host must give you a public `https://` URL and forward WebSockets. Write that URL down.

Railway is one example of a Docker host with volumes; consult its current [volume documentation](https://docs.railway.com/volumes/reference) and plan limits. Render also supports a persistent disk, though its [disk documentation](https://render.com/docs/disks) describes plan requirements. Check the host's terms and costs before deploying.

## 2. Point the static site at the server

Edit `docs/config.js` in your repository. Replace the placeholder with **only** your backend's public HTTPS origin, without a trailing slash:

```js
window.DECIPHER_API_BASE = 'https://your-server.example';
```

This URL is public configuration. Never put a room password, session token, or other secret into a repository file. The site will display a setup message if the backend address is still blank while running on GitHub Pages.

## 3. Publish the interface

Push the extracted project files to a GitHub repository so `docs/index.html` is present. In the repository, choose **Settings → Pages → Deploy from a branch → main → /docs → Save**. Your site will be at `https://YOUR_GITHUB_USERNAME.github.io/REPOSITORY/` (or at your root `github.io` URL if this is a user site). The files use relative paths, so both URL shapes work. See [GitHub's Pages setup instructions](https://docs.github.com/en/pages/getting-started-with-github-pages/creating-a-github-pages-site).

## 4. Check collaboration

Create a room on the Pages URL using a **unique room password** (at least eight characters). Copy its invite link and open it in another browser or private window. Enter the password there. A partner can also choose **Join a room** and enter the room ID from the link plus the password. Type in the same answer and one of the cipher table cells from both windows, then check a question: the answer, cell, and score should appear in both. The invitation link contains only a room ID; your partner needs the password separately. Do not reuse an account password.

If creating a room fails, check that `docs/config.js` has the HTTPS backend URL and the server allows your Pages origin. If a room connects but a PDF page is blank, check the server's persistent volume and browser network errors. If the host restarts without a persistent volume, the PDFs and rooms will be lost.

## Update an existing deployment

Replace the backend code and the `web/` files on the same server, then redeploy it with the **same persistent data volume**. Replace `docs/app.js`, `docs/index.html`, and `docs/style.css` in your GitHub repository and publish the Pages update. Keep your existing `docs/config.js` backend URL. Reopen your existing room after both updates; the server rebuilds older worksheet layouts without deleting saved answers.

## One host option

You may also run the server by itself and use the interface it serves at `/`; the `web/config.js` default targets the same origin. The password and live editing features work there too. GitHub Pages is optional if your Python host already gives you a public HTTPS URL.
