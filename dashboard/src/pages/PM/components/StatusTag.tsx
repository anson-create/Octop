import { Tag } from "antd";
import { useTranslation } from "react-i18next";
import { statusTone } from "../pmMeta";

/** Colour-coded enum badge; unknown values fall back to the raw string. */
export default function StatusTag({ status }: { status: string }) {
  const { t } = useTranslation();
  return (
    <Tag color={statusTone(status)} style={{ marginInlineEnd: 0 }}>
      {t(`pm.status.${status}`, { defaultValue: status })}
    </Tag>
  );
}
