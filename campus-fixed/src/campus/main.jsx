import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App.jsx';
import './campus.css';
class ErrorBoundary extends React.Component {
  state = { error: null };
  static getDerivedStateFromError(error) { return { error }; }
  render() {
    if (this.state.error) return <main className="boot"><h1>页面暂时无法显示</h1><p>请刷新页面；如果问题持续，联系维护者。</p><button onClick={() => location.reload()}>重新载入</button></main>;
    return this.props.children;
  }
}
createRoot(document.getElementById('root')).render(<ErrorBoundary><App /></ErrorBoundary>);
