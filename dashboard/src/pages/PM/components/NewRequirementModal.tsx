import { useEffect, useState } from "react";
import { Form, Input, InputNumber, Modal, Select } from "antd";
import { useTranslation } from "react-i18next";
import { pmApi } from "../../../api/modules/pm";
import type { PmRequirement } from "../../../api/types";
import { apiErrorMessage } from "../../../utils/apiError";
import { message } from "../../../utils/antdMessage";

interface NewRequirementModalProps {
  open: boolean;
  workspaceId: number | null;
  onClose: () => void;
  onCreated: (requirement: PmRequirement) => void;
}

interface FormValues {
  title: string;
  raw_text: string;
  source: string;
  source_ref?: string;
  proposer?: string;
  priority?: number;
}

const SOURCES = ["manual", "jira", "kb", "web", "human"] as const;

/** Intake form: title + the raw text. Everything downstream is derived from these two. */
export default function NewRequirementModal({
  open,
  workspaceId,
  onClose,
  onCreated,
}: NewRequirementModalProps) {
  const { t } = useTranslation();
  const [form] = Form.useForm<FormValues>();
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (open) form.resetFields();
  }, [open, form]);

  const submit = async () => {
    if (!workspaceId) return;
    const values = await form.validateFields();
    setSubmitting(true);
    try {
      const created = await pmApi.createRequirement({
        workspace_id: workspaceId,
        title: values.title.trim(),
        raw_text: values.raw_text.trim(),
        source: values.source,
        source_ref: values.source_ref?.trim() || undefined,
        proposer: values.proposer?.trim() || undefined,
        priority: values.priority ?? undefined,
      });
      message.success(t("pm.newRequirement.created"));
      onCreated(created);
      onClose();
    } catch (err) {
      message.error(apiErrorMessage(err, t("pm.error.load"), t));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      open={open}
      title={t("pm.newRequirement.title")}
      onCancel={onClose}
      onOk={() => void submit()}
      okButtonProps={{ disabled: !workspaceId, loading: submitting }}
      okText={t("pm.actions.create")}
      cancelText={t("pm.actions.cancel")}
      destroyOnClose
    >
      <Form
        form={form}
        layout="vertical"
        initialValues={{ source: "manual", priority: 2 }}
        style={{ marginTop: 8 }}
      >
        <Form.Item
          name="title"
          label={t("pm.fields.title")}
          rules={[{ required: true, message: t("pm.newRequirement.titlePlaceholder") }]}
        >
          <Input placeholder={t("pm.newRequirement.titlePlaceholder")} maxLength={200} />
        </Form.Item>
        <Form.Item
          name="raw_text"
          label={t("pm.fields.rawText")}
          rules={[{ required: true, message: t("pm.newRequirement.rawPlaceholder") }]}
        >
          <Input.TextArea
            placeholder={t("pm.newRequirement.rawPlaceholder")}
            autoSize={{ minRows: 5, maxRows: 12 }}
            maxLength={20000}
          />
        </Form.Item>
        <div style={{ display: "flex", gap: 8 }}>
          <Form.Item name="source" label={t("pm.fields.source")} style={{ flex: 1 }}>
            <Select
              options={SOURCES.map((value) => ({
                value,
                label: t(`pm.sources.${value}`, { defaultValue: value }),
              }))}
            />
          </Form.Item>
          <Form.Item name="source_ref" label={t("pm.fields.sourceRef")} style={{ flex: 1 }}>
            <Input placeholder="CSM-1024" />
          </Form.Item>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <Form.Item name="proposer" label={t("pm.fields.proposer")} style={{ flex: 1 }}>
            <Input />
          </Form.Item>
          <Form.Item name="priority" label={t("pm.fields.priority")} style={{ width: 120 }}>
            <InputNumber min={1} max={5} style={{ width: "100%" }} />
          </Form.Item>
        </div>
      </Form>
    </Modal>
  );
}
