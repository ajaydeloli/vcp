const sections = ["Dashboard", "Screener", "Watchlist", "Data health"];

export default function Dashboard() {
  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark">S</span><span>SEPA Scanner</span></div>
        <nav aria-label="Main navigation">
          {sections.map((section, index) => <a className={index === 0 ? "nav-link active" : "nav-link"} href="#" key={section}><span className="nav-dot" />{section}</a>)}
        </nav>
        <div className="sidebar-footer"><span className="status-dot" />Local workspace</div>
      </aside>
      <section className="content">
        <header className="topbar"><div><p className="eyebrow">MARKET RESEARCH</p><h1>Dashboard</h1></div><button className="quiet-button" type="button">Data not connected</button></header>
        <div className="welcome-card">
          <div><p className="eyebrow accent">SCREENER OVERVIEW</p><h2>Your next great setup starts with a strong trend.</h2><p className="muted">Connect a market data provider to begin scanning the NSE universe.</p></div>
          <div className="empty-chart" aria-hidden="true">{Array.from({ length: 9 }, (_, i) => <span key={i} />)}</div>
        </div>
        <div className="section-heading"><div><p className="eyebrow">TODAY</p><h2>Top rated setups</h2></div><span className="muted">No scan results yet</span></div>
        <div className="empty-state"><div className="empty-icon">↗</div><h3>Nothing to rank yet</h3><p>Once daily data is loaded, qualifying Stage 2 stocks will appear here.</p></div>
        <footer>SEPA Scanner <span>Personal research workspace</span></footer>
      </section>
    </main>
  );
}
