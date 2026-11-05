from trainer.application.ports import LocalStorage, TrainerModelCatalog


class TrainerSettings:
    def __init__(self, storage: LocalStorage, catalog: TrainerModelCatalog,
                 default_model: str | None) -> None:
        self._storage = storage
        self._catalog = catalog
        self._default_model = default_model

    def models(self) -> list[str]:
        models = self._catalog.list_models()
        if not models:
            raise RuntimeError("No models are available through OpenCode. Configure a provider in OpenCode and try again.")
        return models

    def selected_model(self) -> str | None:
        return self._storage.trainer_model() or self._default_model

    def select_model(self, model: str) -> None:
        if model not in self.models():
            raise ValueError("Choose a model available through OpenCode.")
        self._storage.set_trainer_model(model)
