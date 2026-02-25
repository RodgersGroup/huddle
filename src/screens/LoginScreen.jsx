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
      setError('Network error \u2014 please try again');
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
        <div style={{ fontSize: 48, marginBottom: 8 }}>{'\u{1F3E0}'}</div>
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
          <div style={{ fontSize: 36, marginBottom: 12 }}>{'\u{1F4EC}'}</div>
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
