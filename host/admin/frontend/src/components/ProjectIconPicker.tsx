import { createPortal } from "react-dom";
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Box, Code2, Database, FolderKanban, Globe2, ImagePlus, Rocket, Sparkles, Upload,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { api } from "../lib/api";
import { useToast } from "../lib/toast";

const BUILTINS: { value: string; label: string; Icon: LucideIcon }[] = [
  { value: "folder", label: "Project", Icon: FolderKanban },
  { value: "rocket", label: "Rocket", Icon: Rocket },
  { value: "box", label: "Box", Icon: Box },
  { value: "code", label: "Code", Icon: Code2 },
  { value: "globe", label: "Globe", Icon: Globe2 },
  { value: "database", label: "Database", Icon: Database },
  { value: "sparkles", label: "Sparkles", Icon: Sparkles },
];

function builtinIcon(value: string | null | undefined): LucideIcon {
  const key = value?.startsWith("builtin:") ? value.slice("builtin:".length) : "folder";
  return BUILTINS.find(i => i.value === key)?.Icon ?? FolderKanban;
}

export function ProjectIcon({ icon, name, size = 20 }: {
  icon: string | null | undefined; name: string; size?: number;
}) {
  if (icon?.startsWith("data:image/") || icon?.startsWith("https://")) {
    return <img className="project-icon-image" src={icon} alt="" width={size} height={size} />;
  }
  const Icon = builtinIcon(icon);
  return <Icon size={size} aria-label={`${name} icon`} />;
}

interface IconOptions { images: { src: string; label: string }[] }

export function ProjectIconPicker({ projectId, icon, name, size = 22 }: {
  projectId: number; icon: string | null | undefined; name: string; size?: number;
}) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ top: 0, left: 0 });
  const buttonRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const qc = useQueryClient();
  const toast = useToast();

  const { data, isFetching } = useQuery<IconOptions>({
    queryKey: ["project-icon-options", projectId],
    queryFn: () => api.get<IconOptions>(`/api/projects/${projectId}/icon-options`),
    enabled: open,
    staleTime: 5 * 60_000,
  });
  const save = useMutation({
    mutationFn: (next: string) => api.patch(`/api/projects/${projectId}`, { icon: next }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      qc.invalidateQueries({ queryKey: ["project", projectId] });
      setOpen(false);
    },
    onError: e => toast.show(String(e), "fail"),
  });

  useEffect(() => {
    if (!open || !buttonRef.current) return;
    const rect = buttonRef.current.getBoundingClientRect();
    setPosition({ top: rect.bottom + 6, left: Math.min(rect.left, window.innerWidth - 330) });
    const close = (event: MouseEvent) => {
      const node = event.target as Node;
      if (!buttonRef.current?.contains(node) && !popoverRef.current?.contains(node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);

  const upload = (file: File | undefined) => {
    if (!file) return;
    if (!file.type.startsWith("image/") || file.size > 2 * 1024 * 1024) {
      toast.show("Choose an image no larger than 2 MB", "fail");
      return;
    }
    const reader = new FileReader();
    reader.onload = () => save.mutate(String(reader.result));
    reader.onerror = () => toast.show("Could not read that image", "fail");
    reader.readAsDataURL(file);
  };

  return (
    <>
      <button
        ref={buttonRef}
        className="project-icon-button"
        aria-label={`Change ${name} icon`}
        title="Change project icon"
        onClick={event => { event.stopPropagation(); setOpen(v => !v); }}
      >
        <ProjectIcon icon={icon} name={name} size={size} />
      </button>
      {open && createPortal(
        <div
          ref={popoverRef}
          className="project-icon-popover"
          style={{ top: position.top, left: Math.max(8, position.left) }}
          onClick={event => event.stopPropagation()}
        >
          <div className="lbl">Icons</div>
          <div className="project-icon-grid">
            {BUILTINS.map(({ value, label, Icon }) => (
              <button key={value} title={label} disabled={save.isPending}
                onClick={() => save.mutate(`builtin:${value}`)}>
                <Icon size={20} /><span>{label}</span>
              </button>
            ))}
          </div>
          <div className="row project-icon-section-title">
            <span className="lbl">From README</span>
            {isFetching && <span className="spinner" />}
          </div>
          {(data?.images.length ?? 0) > 0 ? (
            <div className="project-readme-images">
              {data!.images.map((image, index) => (
                <button key={`${image.label}-${index}`} title={image.label}
                  disabled={save.isPending} onClick={() => save.mutate(image.src)}>
                  <img src={image.src} alt={image.label} />
                </button>
              ))}
            </div>
          ) : !isFetching && <span className="dim"><ImagePlus size={13} /> No README images found</span>}
          <button className="btn small" style={{ width: "100%", marginTop: "0.75rem", justifyContent: "center" }}
            onClick={() => inputRef.current?.click()} disabled={save.isPending}>
            <Upload size={13} /> Upload image
          </button>
          <input ref={inputRef} hidden type="file" accept="image/png,image/jpeg,image/webp,image/gif,image/svg+xml"
            onChange={event => upload(event.target.files?.[0])} />
        </div>,
        document.body,
      )}
    </>
  );
}
