export function Placeholder(props: { title: string; stage: number; blurb: string }) {
  return (
    <div className="placeholder">
      <div className="card">
        <h1>{props.title}</h1>
        <p className="muted">{props.blurb}</p>
        <p>Arrives in Stage {props.stage} of the development plan.</p>
      </div>
    </div>
  );
}
