import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.tsx'

// Production entry — no MSW in the live Admin Console
ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
