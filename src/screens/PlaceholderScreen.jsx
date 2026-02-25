import React from 'react';
import { useTheme } from '../context/ThemeContext';
import ScreenHeader from '../components/ScreenHeader';

export default function PlaceholderScreen({ onBack, title, icon, message, submessage }) {
  const t = useTheme();

  return (
    <div style={{ padding: '0 20px' }}>
      <ScreenHeader title={title} onBack={onBack} />
      <div style={{ textAlign: 'center', padding: '60px 20px' }}>
        <div style={{ fontSize: 48, marginBottom: 16 }}>{icon}</div>
        <div style={{ fontSize: 15, fontWeight: 500, color: t.textSecondary }}>{message}</div>
        {submessage && <div style={{ fontSize: 13, color: t.textMuted, marginTop: 8 }}>{submessage}</div>}
      </div>
    </div>
  );
}
