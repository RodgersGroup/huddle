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
        return <PlaceholderScreen onBack={goHome} title="Bills" icon={'\u{1F4B5}'} message="Bills view coming soon" />;
      case 'tasks':
        return <PlaceholderScreen onBack={goHome} title="Tasks" icon={'\u26A1'} message="All clear! No tasks right now." submessage="Tasks you create or receive will appear here." />;
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
    window.location.href = '/onboard';
    return null;
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
