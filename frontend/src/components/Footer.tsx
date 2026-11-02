const YEAR = new Date().getFullYear()

export function Footer() {
  return (
    <footer className="footer">
      <div className="footer-inner">
        <p>© {YEAR}. Developed by Bekzhan Bidakhmetov</p>
        <a href="mailto:bidahmetov2002@gmail.com">bidahmetov2002@gmail.com</a>
      </div>
    </footer>
  )
}
