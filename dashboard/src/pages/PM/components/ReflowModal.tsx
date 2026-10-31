import { useState } from "react";
import { Alert, Modal, Select } from "antd";
import { useTranslation } from "react-i18next";
import { pmApi } from "../../../api/modules/pm";
import { apiErrorMessage } from "../../../utils/apiError";
import { message } from "../../../utils/antdMessage";
import { STEP_ORDER, stepLabelKey } from "../pmMeta";

interface ReflowModalProps {
  open: boolean;
  taskId: number;
  onClose: () => void;
  onDone: () => void;
}

/**
 * Re-flow is destructive (steps reset, open cards voided), so it is a separate
 * confirm with the from-step spelled out rather than a one-click menu item.
 */
export default function ReflowModal({
  open,
  taskId,
  onClose,
  onDone,
}: ReflowModalProps) {
  const { t } = useTranslation();
  const [fromStep, setFromStep] = useState<string>("requirement_analysis");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    try {
      const result = await pmApi.reflow(taskId, fromStep);
      message.success(t("pm.reflow.done", { count: result.reset_steps }));
      onDone();
      onClose();
    } catch (err) {
      message.error(apiErrorMessage(err, t("pm.error.load"), t));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      title={t("pm.reflow.title")}
      onCancel={onClose}
      onOk={() => void submit()}
      okText={t("pm.actions.reflow")}
      cancelText={t("pm.actions.cancel")}
      okButtonProps={{ loading: busy }}
      destroyOnClose
    >
      <Alert type="warning" showIcon message={t("pm.reflow.hint")} style={{ marginBottom: 12 }} />
      <Select
        value={fromStep}
        onChange={setFromStep}
        style={{ width: "100%" }}
        options={STEP_ORDER.map((key) => ({
          value: key,
          label: t(stepLabelKey(key)),
        }))}
      />
    </Modal>
  );
}
