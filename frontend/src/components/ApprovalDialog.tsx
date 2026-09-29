export interface ApprovalRequest {
  id: string;
  command: string;
  reason: string;
}

export function ApprovalDialog({ request, onAnswer }: { request: ApprovalRequest; onAnswer: (allow: boolean) => void }) {
  return (
    <div className="modal-backdrop">
      <div className="modal approval" role="alertdialog" aria-labelledby="approval-title">
        <div className="modal-head"><strong id="approval-title">Allow this command?</strong></div>
        <p>The agent wants to run a command that {request.reason}.</p>
        <pre className="command">{request.command}</pre>
        <div className="modal-actions">
          <div className="spacer" />
          <button className="danger" onClick={() => onAnswer(false)} autoFocus>Deny</button>
          <button className="primary" onClick={() => onAnswer(true)}>Allow</button>
        </div>
      </div>
    </div>
  );
}
