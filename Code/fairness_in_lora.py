# -*- coding: utf-8 -*-
"""Fairness in LoRA

Automatically generated by Colab.

Original file is located at
    https://colab.research.google.com/drive/1N2gssLqYzx2oZ7hLYXCBQulyoBOOzYPM

# **Requirements and imports**
"""

# Commented out IPython magic to ensure Python compatibility.
import sys
import os
!pip install --upgrade pip
!git clone https://github.com/kenziyuliu/lora-fairness.git
# %cd lora-fairness
!pip install -r requirement.txt

device='cuda'

from huggingface_hub import login
login(token="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")

import argparse
import collections
import gc
import multiprocessing as mp
import os
import pprint
import random
import sys
import time
from typing import Any, Optional, Dict
import yaml
from pathlib import Path
from loguru import logger
from tqdm import tqdm

####### Set caching directories #######
cache_dir = str(Path('./cache').resolve())
logger.info(f'{cache_dir=}')
os.makedirs(f'{cache_dir}/huggingface', exist_ok=True)
os.makedirs(f'{cache_dir}/torch', exist_ok=True)
os.makedirs(f'{cache_dir}/wandb', exist_ok=True)
# For HuggingFace, default to the user's preference for caching (e.g. set by envvars)
# os.environ['HF_HOME'] = f'{cache_dir}/huggingface'  # NOTE: this also changes where the auth token is kept
# os.environ['HF_DATASETS_CACHE'] = f'{cache_dir}/huggingface'
# os.environ['HUGGINGFACE_HUB_CACHE'] = f'{cache_dir}/huggingface'
# os.environ['TRANSFORMERS_CACHE'] = f'{cache_dir}/huggingface'
os.environ['WANDB_DIR'] = os.environ['WANDB_DATA_DIR'] = f'{cache_dir}/wandb'
os.environ['WANDB_CACHE_DIR'] = os.environ['WANDB_CONFIG_DIR'] = os.environ['WANDB_DIR']
os.environ['TORCH_HOME'] = os.environ['TORCH_HUB'] = f'{cache_dir}/torch'
#######################################

import torch
import torch.nn as nn
import datasets
from datasets import Dataset as ArrowDataset
import numpy as np
import transformers
from transformers import AutoTokenizer, AutoConfig, AutoImageProcessor
from transformers import AutoModelForCausalLM, AutoModelForSequenceClassification, AutoModelForImageClassification
from transformers import PreTrainedModel, PreTrainedTokenizer, set_seed
from transformers import Trainer, TrainingArguments
from peft import get_peft_config, LoraConfig, get_peft_model, PeftModel, TaskType
import wandb

# Custom utils
import utils.data_utils as data_utils
import utils.model_utils as model_utils
import utils.eval_utils as eval_utils
from logger_trainer import LoggerTrainer

import os
import yaml
import subprocess
import torch
from pathlib import Path

def run_accelerate_training(lr: float, epoch: int, seed: int = 42):
    """
    Run accelerate training with the specified parameters using Python subprocess

    Args:
        lr (float): Learning rate
        epoch (int): Number of epochs
        seed (int): Random seed (default: 42)
    """
    # Check if CUDA is available
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is not available. Please ensure you have GPU support enabled in Colab.")

    # Set CUDA device
    device = torch.device("cuda")
    torch.cuda.set_device(0)  # Use first GPU

    # Print GPU information
    print(f"Using GPU: {torch.cuda.get_device_name(0)}")
    print(f"Available GPU memory: {torch.cuda.get_device_properties(0).total_memory / 1024**3:.2f} GB")

    # Ensure all config files exist
    config_files = {
        'deepspeed': 'configs/deepspeed_config.yml',
        'model': 'configs/llama2.yml'
    }

    for name, path in config_files.items():
        if not Path(path).exists():
            raise FileNotFoundError(f"{name} config file not found at {path}")

    # Set environment variables for GPU
    os.environ["CUDA_VISIBLE_DEVICES"] = "0"

    # Construct the command as a list of arguments
    cmd = [
        'accelerate', 'launch',
        '--config_file', config_files['deepspeed'],
        'main.py',
        '--config', config_files['model'],
        '--dataset', 'dlab_hatespeech_age',
        '--finetune', 'lora',
        '--lr', str(lr),
        '--epochs', str(epoch),
        '--run_name', f'dlab-age-llama2-7b-lora-epoch{epoch}-lr{lr}',
        '--wandb',
        '--seed', str(seed),
        '--save_strategy', 'no'
    ]

    # Run the command
    try:
        process = subprocess.run(
            cmd,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "CUDA_VISIBLE_DEVICES": "0"}  # Ensure GPU visibility in subprocess
        )
        print("Command executed successfully")
        print("Output:", process.stdout)

    except subprocess.CalledProcessError as e:
        print("Error executing command:")
        print("Exit code:", e.returncode)
        print("Error output:", e.stderr)

# Example usage
if __name__ == "__main__":
    # Set your hyperparameters
    learning_rate = 1e-4  # Replace with your desired learning rate
    num_epochs = 3      # Replace with your desired number of epochs

    run_accelerate_training(
        lr=learning_rate,
        epoch=num_epochs
    )

import os
import yaml
import subprocess
import torch
from pathlib import Path

def check_deepspeed_config(config_path: str):
    """
    Check and validate DeepSpeed configuration
    """
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)

    # Ensure memory-efficient settings
    recommended_settings = {
        'zero_optimization': {
            'stage': 2,  # or 3 for more memory savings
            'offload_optimizer': {
                'device': 'cpu',
                'pin_memory': True
            },
            'overlap_comm': True,
            'contiguous_gradients': True,
            'reduce_bucket_size': 5e7
        },
        'gradient_accumulation_steps': 4,  # Adjust based on your needs
        'gradient_clipping': 1.0,
        'train_batch_size': 1,  # Start small, increase if memory permits
    }

    return recommended_settings

def run_accelerate_training(lr: float, epoch: int, seed: int = 42, batch_size: int = 1):
    """
    Run accelerate training with memory optimizations

    Args:
        lr (float): Learning rate
        epoch (int): Number of epochs
        seed (int): Random seed (default: 42)
        batch_size (int): Batch size (default: 1)
    """
    # Check if CUDA is available
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is not available. Please ensure you have GPU support enabled in Colab.")

    # Set CUDA device and empty cache
    torch.cuda.empty_cache()
    device = torch.device("cuda")
    torch.cuda.set_device(0)

    # Print GPU information and available memory
    print(f"Using GPU: {torch.cuda.get_device_name(0)}")
    print(f"Total GPU memory: {torch.cuda.get_device_properties(0).total_memory / 1024**3:.2f} GB")
    print(f"Current GPU memory usage: {torch.cuda.memory_allocated() / 1024**3:.2f} GB")

    # Ensure all config files exist
    config_files = {
        'deepspeed': 'configs/deepspeed_config.yml',
        'model': 'configs/llama2.yml'
    }

    for name, path in config_files.items():
        if not Path(path).exists():
            raise FileNotFoundError(f"{name} config file not found at {path}")

    # Get recommended DeepSpeed settings
    recommended_settings = check_deepspeed_config(config_files['deepspeed'])
    print("\nRecommended DeepSpeed settings for memory optimization:")
    print(yaml.dump(recommended_settings))

    # Set environment variables for memory optimization
    os.environ["CUDA_VISIBLE_DEVICES"] = "0"
    os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "max_split_size_mb:512"

    # Construct the command with memory optimization flags
    cmd = [
        'accelerate', 'launch',
        '--config_file', config_files['deepspeed'],
        '--machine_rank', '0',
        '--num_processes', '1',  # Single GPU process
        'main.py',
        '--config', config_files['model'],
        '--dataset', 'dlab_hatespeech_age',
        '--finetune', 'lora',
        '--lr', str(lr),
        '--epochs', str(epoch),
        '--run_name', f'dlab-age-llama2-7b-lora-epoch{epoch}-lr{lr}',
        '--wandb',
        '--seed', str(seed),
        '--save_strategy', 'no',
        '--per_device_train_batch_size', str(batch_size),
        '--gradient_accumulation_steps', '4',
        '--fp16',  # Use mixed precision training
    ]

    # Run the command
    try:
        process = subprocess.run(
            cmd,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ}
        )
        print("Command executed successfully")
        print("Output:", process.stdout)

    except subprocess.CalledProcessError as e:
        print("Error executing command:")
        print("Exit code:", e.returncode)
        print("Error output:", e.stderr)

# Example usage
if __name__ == "__main__":
    # Set your hyperparameters
    learning_rate = 1e-4
    num_epochs = 3
    batch_size = 1  # Start with small batch size

    run_accelerate_training(
        lr=learning_rate,
        epoch=num_epochs,
        batch_size=batch_size
    )

import os
import yaml
import subprocess
import torch
from pathlib import Path

def run_accelerate_training(lr: float, epoch: int, seed: int = 42):
    """
    Run accelerate training with the specified parameters using Python subprocess

    Args:
        lr (float): Learning rate
        epoch (int): Number of epochs
        seed (int): Random seed (default: 42)
    """
    # Check if CUDA is available
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is not available. Please ensure you have GPU support enabled in Colab.")

    # Set CUDA device and empty cache
    torch.cuda.empty_cache()
    device = torch.device("cuda")
    torch.cuda.set_device(0)

    # Print GPU information
    print(f"Using GPU: {torch.cuda.get_device_name(0)}")
    print(f"Total GPU memory: {torch.cuda.get_device_properties(0).total_memory / 1024**3:.2f} GB")

    # Ensure all config files exist
    config_files = {
        'deepspeed': 'configs/deepspeed_config.yml',
        'model': 'configs/llama2.yml'
    }

    for name, path in config_files.items():
        if not Path(path).exists():
            raise FileNotFoundError(f"{name} config file not found at {path}")

    # Set environment variables
    os.environ["CUDA_VISIBLE_DEVICES"] = "0"

    # Construct the command with correct arguments
    cmd = [
        'accelerate', 'launch',
        '--config_file', config_files['deepspeed'],
        'main.py',
        '--config', config_files['model'],
        '--dataset', 'dlab_hatespeech_age',
        '--finetune', 'lora',
        '--lr', str(lr),
        '--epochs', str(epoch),
        '--run_name', f'dlab-age-llama2-7b-lora-epoch{epoch}-lr{lr}',
        '--wandb',
        '--seed', str(seed),
        '--save_strategy', 'no',
        '--bs_per_gpu', '1',           # Use bs_per_gpu instead of per_device_train_batch_size
        '--grad_accum', '4',           # Use grad_accum instead of gradient_accumulation_steps
    ]

    # Run the command
    try:
        process = subprocess.run(
            cmd,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ}
        )
        print("Command executed successfully")
        print("Output:", process.stdout)

    except subprocess.CalledProcessError as e:
        print("Error executing command:")
        print("Exit code:", e.returncode)
        print("Error output:", e.stderr)

# Example usage
if __name__ == "__main__":
    # Set your hyperparameters
    learning_rate = 1e-4
    num_epochs = 3

    run_accelerate_training(
        lr=learning_rate,
        epoch=num_epochs
    )

import os
import yaml
import subprocess
import torch
from pathlib import Path

def run_accelerate_training(lr: float, epoch: int, seed: int = 42):
    """
    Run accelerate training for GPT-2 with the specified parameters

    Args:
        lr (float): Learning rate
        epoch (int): Number of epochs
        seed (int): Random seed (default: 42)
    """
    # Check if CUDA is available
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is not available. Please ensure you have GPU support enabled in Colab.")

    # Set CUDA device and empty cache
    torch.cuda.empty_cache()
    device = torch.device("cuda")
    torch.cuda.set_device(0)

    # Print GPU information
    print(f"Using GPU: {torch.cuda.get_device_name(0)}")
    print(f"Total GPU memory: {torch.cuda.get_device_properties(0).total_memory / 1024**3:.2f} GB")

    # Ensure all config files exist
    config_files = {
        'deepspeed': 'configs/deepspeed_config.yml',
        'model': 'configs/gpt2.yml'  # Changed from llama2.yml to gpt2.yml
    }

    for name, path in config_files.items():
        if not Path(path).exists():
            raise FileNotFoundError(f"{name} config file not found at {path}")

    # Set environment variables
    os.environ["CUDA_VISIBLE_DEVICES"] = "0"

    # Construct the command with correct arguments for GPT-2
    cmd = [
        'accelerate', 'launch',
        '--config_file', config_files['deepspeed'],
        'main.py',
        '--config', config_files['model'],
        '--dataset', 'dlab_hatespeech_age',
        '--finetune', 'lora',
        '--model_base', 'gpt2',  # Specify GPT-2 as the base model
        '--lr', str(lr),
        '--epochs', str(epoch),
        '--run_name', f'dlab-age-gpt2-lora-epoch{epoch}-lr{lr}',
        '--wandb',
        '--seed', str(seed),
        '--save_strategy', 'no',
        '--bs_per_gpu', '4',  # GPT-2 can handle larger batch sizes than LLaMA 2
        '--grad_accum', '4',
        '--lora_rank', '8',  # LoRA hyperparameters suitable for GPT-2
        '--lora_alpha', '16'
    ]

    # Run the command
    try:
        process = subprocess.run(
            cmd,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ}
        )
        print("Command executed successfully")
        print("Output:", process.stdout)

    except subprocess.CalledProcessError as e:
        print("Error executing command:")
        print("Exit code:", e.returncode)
        print("Error output:", e.stderr)

# Example usage
if __name__ == "__main__":
    # Set your hyperparameters - adjusted for GPT-2
    learning_rate = 5e-4  # Slightly higher learning rate for GPT-2
    num_epochs = 3

    run_accelerate_training(
        lr=learning_rate,
        epoch=num_epochs
    )

import os
import yaml
import subprocess
import torch
from pathlib import Path

def create_gpt2_config():
    """Create or update GPT-2 configuration file"""
    gpt2_config = {
        'model_name': 'gpt2',
        'tokenizer': 'gpt2',  # Direct string instead of dict
        'max_length': 512
    }

    config_dir = Path('configs')
    config_dir.mkdir(exist_ok=True)

    with open(config_dir / 'gpt2.yml', 'w') as f:
        yaml.dump(gpt2_config, f, default_flow_style=False)

def run_accelerate_training(lr: float, epoch: int, seed: int = 42):
    """
    Run accelerate training for GPT-2 with the specified parameters

    Args:
        lr (float): Learning rate
        epoch (int): Number of epochs
        seed (int): Random seed (default: 42)
    """
    # Check if CUDA is available
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is not available. Please ensure you have GPU support enabled in Colab.")

    # Set CUDA device and empty cache
    torch.cuda.empty_cache()
    device = torch.device("cuda")
    torch.cuda.set_device(0)

    # Print GPU information
    print(f"Using GPU: {torch.cuda.get_device_name(0)}")
    print(f"Total GPU memory: {torch.cuda.get_device_properties(0).total_memory / 1024**3:.2f} GB")

    # Create/update GPT-2 config file
    create_gpt2_config()

    # Ensure all config files exist
    config_files = {
        'deepspeed': 'configs/deepspeed_config.yml',
        'model': 'configs/gpt2.yml'
    }

    for name, path in config_files.items():
        if not Path(path).exists():
            raise FileNotFoundError(f"{name} config file not found at {path}")

    # Set environment variables
    os.environ["CUDA_VISIBLE_DEVICES"] = "0"

    # Construct the command with correct arguments for GPT-2
    cmd = [
        'accelerate', 'launch',
        '--config_file', config_files['deepspeed'],
        'main.py',
        '--config', config_files['model'],
        '--dataset', 'dlab_hatespeech_age',
        '--finetune', 'lora',
        '--model_base', 'gpt2',
        '--tokenizer', 'gpt2',  # Explicitly specify tokenizer
        '--lr', str(lr),
        '--epochs', str(epoch),
        '--run_name', f'dlab-age-gpt2-lora-epoch{epoch}-lr{lr}',
        '--wandb',
        '--seed', str(seed),
        '--save_strategy', 'no',
        '--bs_per_gpu', '4',
        '--grad_accum', '4',
        '--lora_rank', '8',
        '--lora_alpha', '16'
    ]

    # Run the command
    try:
        process = subprocess.run(
            cmd,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ}
        )
        print("Command executed successfully")
        print("Output:", process.stdout)

    except subprocess.CalledProcessError as e:
        print("Error executing command:")
        print("Exit code:", e.returncode)
        print("Error output:", e.stderr)

# Example usage
if __name__ == "__main__":
    # Set your hyperparameters
    learning_rate = 5e-4
    num_epochs = 3

    run_accelerate_training(
        lr=learning_rate,
        epoch=num_epochs
    )