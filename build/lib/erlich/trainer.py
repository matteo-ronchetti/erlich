import torch
from tqdm import tqdm
import abc

from .logging import TrainLogger
from .saver import ModelSaver

try:
    from apex import amp

    HAS_APEX = True
except ImportError:
    print("[Erlich INFO] Apex not found")
    amp = None
    HAS_APEX = False


def move_to_device(batch, device):
    if isinstance(batch, tuple) or isinstance(batch, list):
        return [x.to(device) for x in batch]
    else:
        return batch.to(device)


class AvgEstimator:
    def __init__(self):
        self.avg = 0.0
        self.tot_weight = 0.0

    def update(self, x, w=1.0):
        self.tot_weight += w
        self.avg += (x - self.avg) * w / self.tot_weight

    def get(self):
        return self.avg


class BaseTrainer(abc.ABC):
    def __init__(self, cfg, model_parts, saver, logger, device):
        self.cfg = cfg
        self.batch_size = cfg.batch_size
        self.validation_batch_size = cfg.validation_batch_size
        self.epochs = cfg.epochs
        self.device = device

        self.optimizers = dict()
        self.model_parts = model_parts
        self.saver = saver
        self.logger = logger

        self.dataloader = self.get_dataloader(self.batch_size)
        self.validation_dataloader = self.get_validation_dataloader(self.validation_batch_size)

        self.train_metrics = self.get_train_metrics()
        if not isinstance(self.train_metrics, list):
            self.train_metrics = [self.train_metrics]

        # Add train metrics to logger
        for metric in self.train_metrics:
            self.logger.add_meter(metric)

    def set_parts(self, parts):
        self.model_parts = parts

    @staticmethod
    def standardize_kwargs(cfg, **kwargs):
        return {k: cfg[k] if k in cfg else kwargs[k] for k in kwargs}

    def default_get_optimizer(self, name, optim_cfg, parameters, cfg):
        if name == "adam":
            cfg = self.standardize_kwargs(optim_cfg, lr=1e-3, betas=(0.9, 0.999), eps=1e-8,
                                          weight_decay=0, amsgrad=False)
            return torch.optim.Adam(parameters, **cfg)
        if name == "sgd":
            cfg = self.standardize_kwargs(optim_cfg, lr=1e-3, momentum=0, dampening=0,
                                          weight_decay=0, nesterov=False)
            return torch.optim.SGD(parameters, **cfg)
        else:
            raise Exception(f"Unkown optimizer '{name}', please override 'get_optimizer' method to add this optimizer")

    def get_optimizer(self, name, optim_cfg, parameters, cfg):
        return self.default_get_optimizer(name, optim_cfg, parameters, cfg)

    def instantiate_optimizers(self, cfg):
        require_global_optimizer = []
        for part_name in cfg.parts:
            part = cfg.parts[part_name]
            # if part requires an optimizer
            if "frozen" not in part or part["frozen"] is False:
                # if part has specific optimizer
                if "optimizer" in part:
                    self.optimizers[part_name] = self.get_optimizer(part.optimizer.name, part.optimizer,
                                                                    self.model_parts[part_name].parameters(), cfg)
                else:
                    require_global_optimizer.append(part_name)

        # if some parts require the global optimizer instantiate it
        if require_global_optimizer:
            global_opt_parameters = []
            for x in require_global_optimizer:
                global_opt_parameters += list(self.model_parts[x].parameters())

            self.optimizers["__global"] = self.get_optimizer(cfg.optimizer.name, cfg.optimizer,
                                                             global_opt_parameters, cfg)

    @abc.abstractmethod
    def train_step(self, batch, batch_idx, train_metrics):
        pass

    def validation_step(self, batch, batch_idx):
        return dict()

    @abc.abstractmethod
    def get_dataloader(self, batch_size):
        pass

    def get_validation_dataloader(self, validation_batch_size):
        return None

    def get_train_metrics(self):
        return []

    def validate(self, epoch, train_batch, use_apex):
        if self.validation_dataloader is not None:
            print("Validating model")
            estimators = dict()
            for batch_idx, batch in enumerate(tqdm(self.validation_dataloader)):
                with torch.no_grad():
                    batch = move_to_device(batch, self.device)

                    metrics = self.validation_step(batch, batch_idx)
                    w = float(metrics.get("weight", 1.0))
                    for k in metrics:
                        if k not in estimators:
                            estimators[k] = AvgEstimator()
                        estimators[k].update(metrics[k], w)

            estimators = {k: estimators[k].get() for k in estimators}
            print(estimators)
        else:
            estimators = dict()

        self.saver.save(self.model_parts, self.optimizers, amp if use_apex else None, epoch, train_batch, estimators)

    def train(self, validate_every=-1, logger_min_wait=5):
        # Define the set of batches IDs after which model is validated
        if validate_every == -1:
            validate_every = set()
        else:
            # exclude last batch because it is validated on epoch
            validate_every = {i for i in range(validate_every, len(self.dataloader), validate_every)}.difference(
                {len(self.dataloader) - 1})

        self.logger.min_wait = logger_min_wait

        using_apex = self.cfg.get("apex", False) and HAS_APEX
        if using_apex:
            optimization_level = self.cfg.apex
            print("")
            print("=" * 20, "APEX", "=" * 20)

            # Convert dicts to lists
            part_keys = sorted(list(self.model_parts.keys()))
            opt_keys = sorted(list(self.optimizers.keys()))
            parts = [self.model_parts[k] for k in part_keys]
            optimizers = [self.optimizers[k] for k in opt_keys]

            parts, optimizers = amp.initialize(parts, optimizers, opt_level=optimization_level)

            # convert back to dicts
            self.model_parts = {k: x for k, x in zip(part_keys, parts)}
            self.optimizers = {k: x for k, x in zip(opt_keys, optimizers)}

        print("\n")
        print("=" * 20, "TRAINING", "=" * 20)
        self.logger.start(self.dataloader)
        for epoch in range(self.epochs):
            for batch_idx, batch in enumerate(self.dataloader):
                # zero grad
                for optim in self.optimizers.values():
                    optim.zero_grad()

                # move data to device
                batch = move_to_device(batch, self.device)

                # do forward step
                loss = self.train_step(batch, batch_idx, self.train_metrics)

                if using_apex:
                    with amp.scale_loss(loss, list(self.optimizers.values())) as scaled_loss:
                        scaled_loss.backward()
                else:
                    loss.backward()

                for optim in self.optimizers.values():
                    optim.step()

                self.logger.batch()

                if batch_idx in validate_every:
                    self.validate(epoch, batch_idx, using_apex)

            self.logger.epoch()
            self.validate(epoch, len(self.dataloader), using_apex)

# class BaseTrainer(abc.ABC):
#     def __init__(self, batch_size, validation_batch_size, epochs, optimizers_cfg, device):
#         self.batch_size = batch_size
#         self.validation_batch_size = validation_batch_size
#         self.epochs = epochs
#         self.optimizers_cfg = optimizers_cfg
#         self.device = device
#
#         self.optimizers = None
#         self.model = None
#         self.logger = None
#         self.saver = None
#         self.train_metrics = []
#
#         self.dataloader = None
#         self.validation_dataloader = None
#
#     @abc.abstractmethod
#     def train_step(self, batch, batch_idx, train_metrics):
#         pass
#
#     def validation_step(self, batch, batch_idx):
#         return dict()
#
#     @abc.abstractmethod
#     def get_dataloader(self, batch_size):
#         pass
#
#     def get_validation_dataloader(self, validation_batch_size):
#         return None
#
#     def get_optimizers(self, model, cfg):
#         return [torch.optim.Adam(model.parameters(), weight_decay=0.0, amsgrad=True)]
#
#     def get_train_metrics(self):
#         return []
#
#     def setup(self, model, logger: TrainLogger, saver: ModelSaver):
#         self.model = model
#         self.model = self.model.to(self.device)
#         self.logger = logger
#         self.saver = saver
#
#         self.optimizers = self.get_optimizers(model, self.optimizers_cfg)
#
#         self.dataloader = self.get_dataloader(self.batch_size)
#         self.validation_dataloader = self.get_validation_dataloader(self.validation_batch_size)
#
#         self.train_metrics = self.get_train_metrics()
#         if not isinstance(self.train_metrics, list):
#             self.train_metrics = [self.train_metrics]
#
#         # Add train metrics to logger
#         for metric in self.train_metrics:
#             self.logger.add_meter(metric)
#
#     def validate(self, epoch, use_apex):
#         validation_metrics = dict()
#         if self.validation_dataloader is not None:
#             print("Validating model")
#             estimators = dict()
#             for batch_idx, batch in enumerate(tqdm(self.validation_dataloader)):
#                 with torch.no_grad():
#                     batch = move_to_device(batch, self.device)
#
#                     metrics = self.validation_step(batch, batch_idx)
#                     w = float(metrics.get("weight", 1.0))
#                     for k in metrics:
#                         if k not in estimators:
#                             estimators[k] = AvgEstimator()
#                         estimators[k].update(metrics[k], w)
#
#             estimators = {k: estimators[k].get() for k in estimators}
#             print(estimators)
#
#         print("Saving model")
#         self.saver.save(self.model, epoch, validation_metrics, self.optimizers, amp if use_apex else None)
#
#     def train(self, validate_every=-1, logger_min_wait=5, use_apex=False, optimization_level="O1"):
#         # Define the set of batches IDs after which model is validated
#         if validate_every == -1:
#             validate_every = set()
#         else:
#             # exclude last batch because it is validated on epoch
#             validate_every = {i for i in range(validate_every, len(self.dataloader), validate_every)}.difference(
#                 {len(self.dataloader) - 1})
#
#         self.logger.min_wait = logger_min_wait
#
#         use_apex = use_apex and HAS_APEX
#         if use_apex:
#             self.model, self.optimizers = amp.initialize(self.model, self.optimizers, opt_level=optimization_level)
#
#         self.logger.start(self.dataloader)
#         for epoch in range(self.epochs):
#             for batch_idx, batch in enumerate(self.dataloader):
#                 # zero grad
#                 for optim in self.optimizers:
#                     optim.zero_grad()
#
#                 # move data to device
#                 batch = move_to_device(batch, self.device)
#
#                 # do forward step
#                 loss = self.train_step(batch, batch_idx, self.train_metrics)
#
#                 if use_apex:
#                     with amp.scale_loss(loss, self.optimizers) as scaled_loss:
#                         scaled_loss.backward()
#                 else:
#                     loss.backward()
#
#                 for optim in self.optimizers:
#                     optim.step()
#
#                 self.logger.batch()
#
#                 if batch_idx in validate_every:
#                     self.validate(epoch, use_apex)
#
#             self.logger.epoch()
#             self.validate(epoch, use_apex)