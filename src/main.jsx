import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import './index.css';
import HuddleApp from './app/HuddleApp';

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <HuddleApp />
  </StrictMode>
);
