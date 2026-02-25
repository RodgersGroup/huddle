# Combine chores-kiosk into huddle — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Merge the chores-kiosk FastAPI backend into the huddle project directory so everything lives under `/home/keiran/huddle/`, then wire the React frontend auth to the existing magic link system so users can log in and stay logged in across refreshes.

**Architecture:** The FastAPI backend moves into `huddle/backend/`. The React app stays in `huddle/src/`. Vite proxies API requests to FastAPI in dev. In production, FastAPI serves the built React app as static files from `backend/static/spa/`. The existing SQLite database, session cookies, and magic link auth are preserved unchanged.

**Tech Stack:** FastAPI + SQLite (backend), React 19 + Vite (frontend), itsdangerous (sessions), Resend (magic link emails)

---

### Task 1: Copy backend files into huddle/backend/

**Files:**
- Create: `huddle/backend/` (directory with all Python backend files)

**Step 1: Create backend directory and copy all Python source files**

```bash
mkdir -p /home/keiran/huddle/backend

# Copy core Python files
cp /home/keiran/chores-kiosk/app.py /home/keiran/huddle/backend/
cp /home/keiran/chores-kiosk/auth.py /home/keiran/huddle/backend/
cp /home/keiran/chores-kiosk/db.py /home/keiran/huddle/backend/
cp /home/keiran/chores-kiosk/config.py /home/keiran/huddle/backend/
cp /home/keiran/chores-kiosk/settings.py /home/keiran/huddle/backend/
cp /home/keiran/chores-kiosk/websocket.py /home/keiran/huddle/backend/
cp /home/keiran/chores-kiosk/background.py /home/keiran/huddle/backend/
cp /home/keiran/chores-kiosk/push.py /home/keiran/huddle/backend/
cp /home/keiran/chores-kiosk/logging_config.py /home/keiran/huddle/backend/
cp /home/keiran/chores-kiosk/tiers.py /home/keiran/huddle/backend/
cp /home/keiran/chores-kiosk/page_views.py /home/keiran/huddle/backend/
cp /home/keiran/chores-kiosk/fuel.py /home/keiran/huddle/backend/ 2>/dev/null
cp /home/keiran/chores-kiosk/fuel_app.py /home/keiran/huddle/backend/ 2>/dev/null
cp /home/keiran/chores-kiosk/ollama_helper.py /home/keiran/huddle/backend/ 2>/dev/null

# Copy directories
cp -r /home/keiran/chores-kiosk/routers /home/keiran/huddle/backend/
cp -r /home/keiran/chores-kiosk/migrations /home/keiran/huddle/backend/
cp -r /home/keiran/chores-kiosk/helpers /home/keiran/huddle/backend/
cp -r /home/keiran/chores-kiosk/templates /home/keiran/huddle/backend/
cp -r /home/keiran/chores-kiosk/static /home/keiran/huddle/backend/
cp -r /home/keiran/chores-kiosk/scripts /home/keiran/huddle/backend/
cp -r /home/keiran/chores-kiosk/tests /home/keiran/huddle/backend/

# Copy config/secret files
cp /home/keiran/chores-kiosk/.env /home/keiran/huddle/backend/
cp /home/keiran/chores-kiosk/.env.example /home/keiran/huddle/backend/
cp /home/keiran/chores-kiosk/vapid_keys.json /home/keiran/huddle/backend/
cp /home/keiran/chores-kiosk/requirements.txt /home/keiran/huddle/backend/

# Copy the live database
cp /home/keiran/chores-kiosk/chores.db /home/keiran/huddle/backend/
cp /home/keiran/chores-kiosk/chores.db-shm /home/keiran/huddle/backend/ 2>/dev/null
cp /home/keiran/chores-kiosk/chores.db-wal /home/keiran/huddle/backend/ 2>/dev/null

# Copy systemd service file for reference
cp /home/keiran/chores-kiosk/chores-kiosk.service /home/keiran/huddle/backend/
```

**Step 2: Remove __pycache__ directories from the copy**

```bash
find /home/keiran/huddle/backend -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null
```

**Step 3: Verify all critical files exist**

```bash
ls /home/keiran/huddle/backend/app.py /home/keiran/huddle/backend/auth.py /home/keiran/huddle/backend/db.py /home/keiran/huddle/backend/config.py /home/keiran/huddle/backend/chores.db /home/keiran/huddle/backend/.env
```

Expected: All files listed, no errors.

**Step 4: Commit**

```bash
cd /home/keiran/huddle
git add backend/
git commit -m "feat: copy chores-kiosk backend into huddle/backend/"
```

---

### Task 2: Set up Python virtualenv in huddle/backend/

**Files:**
- Create: `huddle/backend/venv/` (virtualenv)

**Step 1: Create virtualenv and install dependencies**

```bash
cd /home/keiran/huddle/backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install itsdangerous resend Pillow sdnotify
```

**Step 2: Verify the backend starts**

```bash
cd /home/keiran/huddle/backend
source venv/bin/activate
python -m uvicorn app:app --host 127.0.0.1 --port 8002 &
sleep 3
curl -s http://127.0.0.1:8002/api/auth/me | head -c 200
kill %1
```

Expected: Should get a 401 JSON response (not authenticated), proving the backend runs.

---

### Task 3: Update db.py to use backend-relative paths

The `db.py` uses `Path(__file__).parent` for `BASE_DIR`, which already resolves to wherever the file lives. Same for `config.py` and its `.env` loader. **Verify these work from the new location — no changes should be needed** since they use `__file__`-relative paths.

**Step 1: Verify db.py path resolution**

```bash
cd /home/keiran/huddle/backend
source venv/bin/activate
python -c "from db import DB_PATH; print(DB_PATH); print(DB_PATH.exists())"
```

Expected: `/home/keiran/huddle/backend/chores.db` and `True`

**Step 2: Verify config.py loads .env**

```bash
cd /home/keiran/huddle/backend
source venv/bin/activate
python -c "from config import SECRET_KEY, ENVIRONMENT; print(f'SECRET_KEY={SECRET_KEY[:8]}... ENV={ENVIRONMENT}')"
```

Expected: Shows the first 8 chars of the secret key and the environment.

---

### Task 4: Configure Vite to proxy API requests to FastAPI

**Files:**
- Modify: `huddle/vite.config.js`

**Step 1: Update vite.config.js with proxy configuration**

Add a `server.proxy` config so that during development, all `/api/*`, `/auth/*`, and `/ws` requests are forwarded to the FastAPI backend running on port 8001.

```javascript
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8001',
      '/auth': 'http://127.0.0.1:8001',
      '/ws': {
        target: 'ws://127.0.0.1:8001',
        ws: true,
      },
    },
  },
});
```

**Step 2: Commit**

```bash
git add vite.config.js
git commit -m "feat: configure Vite proxy for FastAPI backend"
```

---

### Task 5: Add AuthContext to the React app

**Files:**
- Create: `huddle/src/context/AuthContext.jsx`

This context calls `/api/auth/me` on mount to check if the user is logged in. If the existing `huddle_session` cookie is present (set by magic link verification), the backend will return user info. The context exposes `{ user, loading, logout }`.

**Step 1: Create AuthContext.jsx**

```jsx
import { createContext, useContext, useState, useEffect, useCallback } from 'react';

const AuthContext = createContext();

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch('/api/auth/me', { credentials: 'include' })
      .then((res) => {
        if (res.ok) return res.json();
        return null;
      })
      .then((data) => {
        if (data) setUser(data);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const logout = useCallback(async () => {
    await fetch('/api/auth/logout', {
      method: 'POST',
      credentials: 'include',
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
    });
    setUser(null);
  }, []);

  const refreshUser = useCallback(async () => {
    try {
      const res = await fetch('/api/auth/me', { credentials: 'include' });
      if (res.ok) {
        const data = await res.json();
        setUser(data);
        return data;
      }
    } catch {}
    setUser(null);
    return null;
  }, []);

  return (
    <AuthContext.Provider value={{ user, loading, logout, refreshUser }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within an AuthProvider');
  return ctx;
}
```

**Step 2: Commit**

```bash
git add src/context/AuthContext.jsx
git commit -m "feat: add AuthContext with /api/auth/me session check"
```

---

### Task 6: Add LoginScreen to the React app

**Files:**
- Create: `huddle/src/screens/LoginScreen.jsx`

This screen shows a magic link login form. User enters email, we POST to `/api/auth/magic-link`, and show a "check your email" message. This matches the existing backend endpoint.

**Step 1: Create LoginScreen.jsx**

```jsx
import { useState } from 'react';
import { useTheme } from '../context/ThemeContext';

export default function LoginScreen() {
  const t = useTheme();
  const [email, setEmail] = useState('');
  const [sent, setSent] = useState(false);
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    setSubmitting(true);
    try {
      const res = await fetch('/api/auth/magic-link', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Requested-With': 'XMLHttpRequest',
        },
        credentials: 'include',
        body: JSON.stringify({ email }),
      });
      const data = await res.json();
      if (res.ok) {
        setSent(true);
      } else {
        setError(data.detail || 'Something went wrong');
      }
    } catch {
      setError('Network error — please try again');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div style={{
      width: '100%', maxWidth: 420, margin: '0 auto', minHeight: '100vh',
      background: t.bg, display: 'flex', flexDirection: 'column',
      alignItems: 'center', justifyContent: 'center', padding: 24,
      fontFamily: '-apple-system, BlinkMacSystemFont, "SF Pro Display", "Segoe UI", sans-serif',
    }}>
      <div style={{ textAlign: 'center', marginBottom: 40 }}>
        <div style={{ fontSize: 48, marginBottom: 8 }}>🏠</div>
        <h1 style={{ fontSize: 28, fontWeight: 700, color: t.text, margin: 0, letterSpacing: '-0.02em' }}>
          Huddle
        </h1>
        <p style={{ fontSize: 14, color: t.textMuted, marginTop: 6 }}>
          Your household, organised
        </p>
      </div>

      {sent ? (
        <div style={{
          background: t.card, border: `1px solid ${t.cardBorder}`, borderRadius: 16,
          padding: 32, textAlign: 'center', width: '100%', boxShadow: t.cardShadow,
        }}>
          <div style={{ fontSize: 36, marginBottom: 12 }}>📬</div>
          <h2 style={{ fontSize: 18, fontWeight: 600, color: t.text, margin: '0 0 8px' }}>
            Check your email
          </h2>
          <p style={{ fontSize: 14, color: t.textMuted, lineHeight: 1.5, margin: 0 }}>
            We sent a magic link to <strong style={{ color: t.text }}>{email}</strong>.
            Click it to sign in.
          </p>
          <button
            onClick={() => { setSent(false); setEmail(''); }}
            style={{
              marginTop: 20, background: 'none', border: 'none', color: '#4ecdc4',
              fontSize: 14, fontWeight: 600, cursor: 'pointer',
            }}
          >
            Use a different email
          </button>
        </div>
      ) : (
        <form onSubmit={handleSubmit} style={{ width: '100%' }}>
          <div style={{
            background: t.card, border: `1px solid ${t.cardBorder}`, borderRadius: 16,
            padding: 24, boxShadow: t.cardShadow,
          }}>
            <label style={{ fontSize: 14, fontWeight: 600, color: t.text, display: 'block', marginBottom: 8 }}>
              Email address
            </label>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
              required
              autoFocus
              style={{
                width: '100%', padding: '12px 14px', fontSize: 16, borderRadius: 10,
                border: `1px solid ${t.cardBorder}`, background: t.bg, color: t.text,
                outline: 'none', boxSizing: 'border-box',
              }}
            />
            {error && (
              <p style={{ fontSize: 13, color: '#ef4444', marginTop: 8, marginBottom: 0 }}>{error}</p>
            )}
            <button
              type="submit"
              disabled={submitting}
              style={{
                width: '100%', marginTop: 16, padding: '13px 0', fontSize: 15, fontWeight: 600,
                borderRadius: 12, border: 'none', cursor: submitting ? 'wait' : 'pointer',
                background: '#4ecdc4', color: '#fff',
                opacity: submitting ? 0.7 : 1,
              }}
            >
              {submitting ? 'Sending...' : 'Send magic link'}
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
```

**Step 2: Commit**

```bash
git add src/screens/LoginScreen.jsx
git commit -m "feat: add LoginScreen with magic link email form"
```

---

### Task 7: Wire AuthProvider into HuddleApp and gate behind login

**Files:**
- Modify: `huddle/src/app/HuddleApp.jsx`

Wrap the app in `AuthProvider`. If loading, show a spinner. If not authenticated, show `LoginScreen`. Otherwise show the normal app.

**Step 1: Update HuddleApp.jsx**

Replace the existing `HuddleApp` export to wrap everything in `AuthProvider` and conditionally render `LoginScreen` vs `AppShell`.

```jsx
import React, { useState, useRef, useCallback } from 'react';
import { ThemeProvider, useTheme } from '../context/ThemeContext';
import { DataProvider } from '../context/DataContext';
import { AuthProvider, useAuth } from '../context/AuthContext';
import BottomNav from '../components/BottomNav';
import HomeScreen from '../screens/HomeScreen';
import ChoresScreen from '../screens/ChoresScreen';
import CalendarScreen from '../screens/CalendarScreen';
import ShoppingScreen from '../screens/ShoppingScreen';
import MealsScreen from '../screens/MealsScreen';
import PlaceholderScreen from '../screens/PlaceholderScreen';
import LoginScreen from '../screens/LoginScreen';

function AppShell() {
  const t = useTheme();
  const [screen, setScreen] = useState('home');
  const [transitioning, setTransitioning] = useState(false);
  const containerRef = useRef(null);
  const timeoutRef = useRef(null);

  const navigate = useCallback((target) => {
    if (timeoutRef.current) {
      clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
    }
    setTransitioning(true);
    timeoutRef.current = setTimeout(() => {
      setScreen(target);
      setTransitioning(false);
      timeoutRef.current = null;
      if (containerRef.current) containerRef.current.scrollTop = 0;
    }, 150);
  }, []);

  const goHome = useCallback(() => navigate('home'), [navigate]);

  const renderScreen = () => {
    switch (screen) {
      case 'home':
        return <HomeScreen onNavigate={navigate} />;
      case 'chores':
        return <ChoresScreen onBack={goHome} />;
      case 'calendar':
        return <CalendarScreen onBack={goHome} />;
      case 'shopping':
        return <ShoppingScreen onBack={goHome} />;
      case 'meals':
        return <MealsScreen onBack={goHome} />;
      case 'bills':
        return <PlaceholderScreen onBack={goHome} title="Bills" icon={'💵'} message="Bills view coming soon" />;
      case 'tasks':
        return <PlaceholderScreen onBack={goHome} title="Tasks" icon={'⚡'} message="All clear! No tasks right now." submessage="Tasks you create or receive will appear here." />;
      default:
        return <HomeScreen onNavigate={navigate} />;
    }
  };

  return (
    <div style={{
      width: '100%', maxWidth: 420, margin: '0 auto', height: '100vh',
      background: t.bg,
      fontFamily: '-apple-system, BlinkMacSystemFont, "SF Pro Display", "Segoe UI", sans-serif',
      position: 'relative', overflow: 'hidden',
      borderLeft: `1px solid ${t.cardBorder}`, borderRight: `1px solid ${t.cardBorder}`,
      transition: 'background 0.3s ease',
    }}>
      <div ref={containerRef} style={{
        height: '100%', overflowY: 'auto', overflowX: 'hidden',
        paddingBottom: 80,
        opacity: transitioning ? 0 : 1,
        transform: transitioning ? 'translateY(8px)' : 'translateY(0)',
        transition: 'opacity 0.15s ease, transform 0.15s ease',
        WebkitOverflowScrolling: 'touch',
      }}>
        {renderScreen()}
      </div>
      <BottomNav currentScreen={screen} onNavigate={navigate} />
    </div>
  );
}

function AuthGate() {
  const t = useTheme();
  const { user, loading } = useAuth();

  if (loading) {
    return (
      <div style={{
        width: '100%', maxWidth: 420, margin: '0 auto', height: '100vh',
        background: t.bg, display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}>
        <div style={{ color: t.textMuted, fontSize: 15, fontWeight: 500 }}>Loading...</div>
      </div>
    );
  }

  if (!user) {
    return <LoginScreen />;
  }

  return (
    <DataProvider>
      <AppShell />
    </DataProvider>
  );
}

export default function HuddleApp() {
  return (
    <ThemeProvider>
      <AuthProvider>
        <AuthGate />
      </AuthProvider>
    </ThemeProvider>
  );
}
```

**Step 2: Commit**

```bash
git add src/app/HuddleApp.jsx
git commit -m "feat: gate app behind auth — show LoginScreen when not logged in"
```

---

### Task 8: Handle magic link callback in the React app

**Files:**
- Modify: `huddle/src/context/AuthContext.jsx`

The magic link verification endpoint (`GET /auth/verify?token=XXX`) is handled by the FastAPI backend. It sets the session cookie and redirects to `/mobile`. We need the React app to handle the post-redirect: when the user lands on the app after clicking the magic link, the session cookie is already set, so `AuthContext`'s `/api/auth/me` call will succeed.

However, we need to make sure the backend redirects to `/` (where the React SPA lives) instead of `/mobile`.

**Step 1: Update the auth verify redirect in backend**

In `huddle/backend/auth.py`, find the redirect after successful magic link verification. The backend currently redirects to `/mobile` or `/onboard`. We need to add a redirect to `/` for the React SPA.

Find the two `RedirectResponse` calls in the verify endpoint:
- `RedirectResponse(url="/mobile", ...)` → keep as-is (old template users)
- Add: If the request has a `Referer` or query param indicating SPA, redirect to `/`

Actually, the simplest approach: the existing redirect to `/mobile` is fine. We just need the Vite proxy or production setup to serve the React app at `/`. The session cookie will be set regardless of where the redirect goes. After redirect, the React app loads and `/api/auth/me` succeeds.

For now, the easiest fix: **add a catch-all in the React app** that checks `window.location.search` for `?verified=1` (we'll add this to the backend redirect), and re-check auth.

Actually, even simpler: The existing flow works as-is. User clicks magic link → backend sets cookie → redirects to `/mobile` → if we serve the React SPA at `/mobile` in production, it works. But for now, the user can just navigate to the SPA root after clicking the link.

**The cleanest approach**: Update the backend verify endpoint to redirect to `/` when accessed from the SPA context. We add a `redirect` query param to the magic link URL.

In `huddle/backend/auth.py`, modify the verify endpoint to check for a `redirect` param:

Find the line (approximately line 595-615 area):
```python
response = RedirectResponse(url="/mobile", status_code=302)
```

Change to:
```python
redirect_url = request.query_params.get("redirect", "/mobile")
# Only allow relative redirects (prevent open redirect)
if not redirect_url.startswith("/"):
    redirect_url = "/mobile"
response = RedirectResponse(url=redirect_url, status_code=302)
```

And do the same for the second RedirectResponse (the no-household case, redirects to `/onboard`).

**Step 2: Commit**

```bash
cd /home/keiran/huddle
git add backend/auth.py
git commit -m "feat: allow redirect param on magic link verify for SPA support"
```

---

### Task 9: Add a `huddle.service` systemd unit for the new location

**Files:**
- Create: `huddle/backend/huddle.service`

**Step 1: Create the new systemd service file**

```ini
[Unit]
Description=Huddle Household Manager
After=network.target

[Service]
Type=simple
User=keiran
WorkingDirectory=/home/keiran/huddle/backend
ExecStart=/home/keiran/huddle/backend/venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8001
Restart=always
RestartSec=5
WatchdogSec=60
StandardOutput=journal
StandardError=journal
Environment=ENVIRONMENT=production

[Install]
WantedBy=multi-user.target
```

**Step 2: Commit**

```bash
git add backend/huddle.service
git commit -m "feat: add huddle.service systemd unit for new backend location"
```

---

### Task 10: Update .gitignore for the combined project

**Files:**
- Create: `huddle/.gitignore`

**Step 1: Create .gitignore**

```
node_modules/
dist/
backend/venv/
backend/chores.db
backend/chores.db-shm
backend/chores.db-wal
backend/.env
backend/vapid_keys.json
backend/logs/
backend/backups/
backend/static/avatars/
backend/static/feedback_screenshots/
backend/static/task_photos/
backend/__pycache__/
backend/routers/__pycache__/
backend/migrations/__pycache__/
backend/helpers/__pycache__/
**/__pycache__/
*.pyc
.DS_Store
```

**Step 2: Commit**

```bash
git add .gitignore
git commit -m "chore: add .gitignore for combined project"
```

---

### Task 11: Update scripts/backup.sh paths

**Files:**
- Modify: `huddle/backend/scripts/backup.sh`

**Step 1: Update paths in backup.sh**

Change `DB_PATH` and `BACKUP_DIR` references from `/home/keiran/chores-kiosk/` to `/home/keiran/huddle/backend/`.

**Step 2: Similarly update watchdog.py and tunnel_health.py and weekly_maintenance.py**

Update any hardcoded paths referencing `/home/keiran/chores-kiosk/` to `/home/keiran/huddle/backend/`.

**Step 3: Commit**

```bash
git add backend/scripts/
git commit -m "chore: update script paths from chores-kiosk to huddle/backend"
```

---

### Task 12: Verify existing users can still log in end-to-end

**Step 1: Start the backend from the new location**

```bash
cd /home/keiran/huddle/backend
source venv/bin/activate
python -m uvicorn app:app --host 127.0.0.1 --port 8001
```

**Step 2: In another terminal, start the Vite dev server**

```bash
cd /home/keiran/huddle
npm run dev
```

**Step 3: Manual verification checklist**

1. Open the Vite dev URL (e.g. `http://localhost:5173`)
2. Should see LoginScreen (not authenticated)
3. Enter an existing user's email and submit
4. Check email for magic link
5. Click magic link → backend sets cookie → redirects
6. Navigate back to the Vite dev URL
7. Should now see the app (HomeScreen) — authenticated
8. Refresh the page — should still be logged in (cookie persists)
9. Test logout

**Step 4: Commit any fixes needed**

---

### Task 13: Final cleanup commit

**Step 1: Remove the old huddle-redesign.jsx reference file**

```bash
rm /home/keiran/huddle/huddle-redesign.jsx
```

**Step 2: Final commit**

```bash
git add -A
git commit -m "chore: remove old monolith reference file, finalize combined project"
```
