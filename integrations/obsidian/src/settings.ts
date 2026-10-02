import { Notice, PluginSettingTab, Setting, type App } from "obsidian";

import type OnyxPlugin from "./main";

export interface OnyxSettings {
  serviceUrl: string;
  contextFolder: string;
}

export const DEFAULT_SETTINGS: OnyxSettings = {
  serviceUrl: "http://127.0.0.1:8899",
  contextFolder: "",
};

export class OnyxSettingTab extends PluginSettingTab {
  constructor(
    app: App,
    private plugin: OnyxPlugin,
  ) {
    super(app, plugin);
  }

  display(): void {
    const { containerEl } = this;
    containerEl.empty();

    new Setting(containerEl)
      .setName("Service URL")
      .setDesc("Where the Onyx app is listening. Keep this on loopback.")
      .addText((text) =>
        text
          .setPlaceholder(DEFAULT_SETTINGS.serviceUrl)
          .setValue(this.plugin.settings.serviceUrl)
          .onChange(async (value) => {
            this.plugin.settings.serviceUrl = value.trim() || DEFAULT_SETTINGS.serviceUrl;
            await this.plugin.saveSettings();
          }),
      );

    new Setting(containerEl)
      .setName("Context folder")
      .setDesc(
        "Folder the model may read as supporting evidence. Defaults to this vault. It must be an allowed root in Onyx.",
      )
      .addText((text) =>
        text
          .setPlaceholder(this.plugin.vaultPath() ?? "/path/to/folder")
          .setValue(this.plugin.settings.contextFolder)
          .onChange(async (value) => {
            this.plugin.settings.contextFolder = value.trim();
            await this.plugin.saveSettings();
          }),
      );

    new Setting(containerEl)
      .setName("Allow this vault as context")
      .setDesc("Registers the context folder with Onyx so answers can cite your notes.")
      .addButton((button) =>
        button
          .setButtonText("Allow vault folder")
          .setCta()
          .onClick(async () => {
            const folder = this.plugin.contextFolder();
            if (!folder) {
              new Notice("This vault is not stored on the local filesystem.");
              return;
            }
            try {
              await this.plugin.service.ensureRoot(folder);
              new Notice(`Onyx can now read ${folder}`);
            } catch (error) {
              new Notice(error instanceof Error ? error.message : String(error));
            }
          }),
      );

    new Setting(containerEl)
      .setName("Markdown and sidebar appearance")
      .setDesc("Automatically shares this vault’s reading styles and file explorer look with Onyx. The app uses the ones from its configured vault and keeps them when Obsidian is closed.")
      .addButton((button) =>
        button.setButtonText("Sync now").onClick(async () => {
          button.setDisabled(true);
          try {
            await this.plugin.syncMarkdownTheme(true);
            new Notice(this.plugin.sidebarError
              ? `Markdown appearance synced. Sidebar appearance failed: ${this.plugin.sidebarError}`
              : "Markdown and sidebar appearance synced. Onyx updates within a few seconds.");
          } catch (error) {
            new Notice(error instanceof Error ? error.message : String(error));
          } finally {
            button.setDisabled(false);
          }
        }),
      );

    new Setting(containerEl)
      .setName("Connection")
      .setDesc("Check that the service is running and reports a compatible version.")
      .addButton((button) =>
        button.setButtonText("Test connection").onClick(async () => {
          try {
            const session = await this.plugin.service.ensureSession(true);
            new Notice(`Onyx ${session.version} · ${session.provider}/${session.model}`);
          } catch (error) {
            new Notice(error instanceof Error ? error.message : String(error));
          }
        }),
      );
  }
}
