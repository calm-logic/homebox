import { useState } from "react";
import { HelpCircle } from "lucide-react";
import { Modal } from "./Modal";

/**
 * Small circular (?) button that sits inline in a page header next to the h1.
 * Clicking it opens a modal explaining what the page does.
 */
export default function PageHelp({ title, children }: { title: string; children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        className="page-help-btn"
        aria-label="About this page"
        onClick={() => setOpen(true)}
      >
        <HelpCircle size={16} />
      </button>
      <Modal open={open} onClose={() => setOpen(false)} title={title}>
        <div className="page-help-body">{children}</div>
      </Modal>
    </>
  );
}
