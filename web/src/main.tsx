import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import { installPwaLifecycle } from './pwa'
import './styles.css'
import './monitoring.css'

createRoot(document.getElementById('root')!).render(<StrictMode><BrowserRouter><App /></BrowserRouter></StrictMode>)

if ('serviceWorker' in navigator && import.meta.env.PROD) {
  window.addEventListener('load', () => {
    installPwaLifecycle((state) => window.dispatchEvent(new CustomEvent('roomos:pwa-update', { detail: state })))
  })
}
