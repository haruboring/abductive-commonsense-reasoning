#!/usr/bin/env python3
# coding=utf-8
# Copyright 2018 Google AI, Google Brain and Carnegie Mellon University Authors and the HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
""" Conditional text generation with the auto-regressive models of the library (GPT/GPT-2/Transformer-XL/XLNet)
"""
from __future__ import absolute_import, division, print_function, unicode_literals

import argparse
import json
import logging

import numpy as np
import torch
import torch.nn.functional as F
import tqdm
from pytorch_transformers import (
    GPT2Config,
    GPT2LMHeadModel,
    GPT2Tokenizer,
    OpenAIGPTConfig,
    OpenAIGPTLMHeadModel,
    OpenAIGPTTokenizer,
    TransfoXLConfig,
    TransfoXLLMHeadModel,
    TransfoXLTokenizer,
    XLNetConfig,
    XLNetLMHeadModel,
    XLNetTokenizer,
)

import comet.interactive.functions as comet_interactive
from anlg.models import GPT2CometLMHeadModel
from anlg.run_lm_finetuning import anli_record_to_gpt_prompt, record_to_text_tokens_with_comet_pred
from anlg.tokenizers import AnliCometGpt2Tokenizer, AnliGpt2Tokenizer
from utils.file_utils import read_jsonl_lines, read_lines, write_items

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s", datefmt="%m/%d/%Y %H:%M:%S", level=logging.INFO
)
logger = logging.getLogger(__name__)

logging.getLogger("pytorch_transformers.tokenization_utils").setLevel(logging.CRITICAL)

MAX_LENGTH = int(10000)  # Hardcoded max length to avoid infinite loop

ALL_MODELS = sum(
    (
        tuple(conf.pretrained_config_archive_map.keys())
        for conf in (GPT2Config, OpenAIGPTConfig, XLNetConfig, TransfoXLConfig)
    ),
    (),
)

MODEL_CLASSES = {
    "gpt2": (GPT2LMHeadModel, GPT2Tokenizer),
    "openai-gpt": (OpenAIGPTLMHeadModel, OpenAIGPTTokenizer),
    "xlnet": (XLNetLMHeadModel, XLNetTokenizer),
    "transfo-xl": (TransfoXLLMHeadModel, TransfoXLTokenizer),
    "gpt2_for_anli": (GPT2CometLMHeadModel, AnliGpt2Tokenizer),
    "gpt2_for_anli_comet": (GPT2CometLMHeadModel, AnliCometGpt2Tokenizer),
}

# Padding text to help Transformer-XL and XLNet with short prompts as proposed by Aman Rusia
# in https://github.com/rusiaaman/XLNet-gen#methodology
# and https://medium.com/@amanrusia/xlnet-speaks-comparison-to-gpt-2-ea1a4e9ba39e
PADDING_TEXT = """ In 1991, the remains of Russian Tsar Nicholas II and his family
(except for Alexei and Maria) are discovered.
The voice of Nicholas's young son, Tsarevich Alexei Nikolaevich, narrates the
remainder of the story. 1883 Western Siberia,
a young Grigori Rasputin is asked by his father and a group of men to perform magic.
Rasputin has a vision and denounces one of the men as a horse thief. Although his
father initially slaps him for making such an accusation, Rasputin watches as the
man is chased outside and beaten. Twenty years later, Rasputin sees a vision of
the Virgin Mary, prompting him to become a priest. Rasputin quickly becomes famous,
with people, even a bishop, begging for his blessing. <eod> </s> <eos>"""


def set_seed(args):
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)


def top_k_top_p_filtering(logits, top_k=0, top_p=0.0, filter_value=-float("Inf")):
    """Filter a distribution of logits using top-k and/or nucleus (top-p) filtering
    Args:
        logits: logits distribution shape (vocabulary size)
        top_k > 0: keep only top k tokens with highest probability (top-k filtering).
        top_p > 0.0: keep the top tokens with cumulative probability >= top_p (nucleus filtering).
            Nucleus filtering is described in Holtzman et al. (http://arxiv.org/abs/1904.09751)
    From: https://gist.github.com/thomwolf/1a5a29f6962089e871b94cbd09daf317
    """
    assert logits.dim() == 1  # batch size 1 for now - could be updated for more but the code would be less clear
    top_k = min(top_k, logits.size(-1))  # Safety check
    if top_k > 0:
        # Remove all tokens with a probability less than the last token of the top-k
        indices_to_remove = logits < torch.topk(logits, top_k)[0][..., -1, None]
        logits[indices_to_remove] = filter_value

    if top_p > 0.0:
        sorted_logits, sorted_indices = torch.sort(logits, descending=True)
        cumulative_probs = torch.cumsum(F.softmax(sorted_logits, dim=-1), dim=-1)

        # Remove tokens with cumulative probability above the threshold
        sorted_indices_to_remove = cumulative_probs > top_p
        # Shift the indices to the right to keep also the first token above the threshold
        sorted_indices_to_remove[..., 1:] = sorted_indices_to_remove[..., :-1].clone()
        sorted_indices_to_remove[..., 0] = 0

        indices_to_remove = sorted_indices[sorted_indices_to_remove]
        logits[indices_to_remove] = filter_value
    return logits


def sample_sequence(
    model,
    length,
    context,
    num_samples=1,
    temperature=1,
    top_k=0,
    top_p=0.0,
    is_xlnet=False,
    device="cpu",
    comet_input=None,
    comet_mask=None,
):
    context = torch.tensor(context, dtype=torch.long, device=device)
    context = context.unsqueeze(0).repeat(num_samples, 1)

    if comet_input is not None:
        comet_input = torch.tensor(comet_input, dtype=torch.long, device=device)
        comet_input = comet_input.unsqueeze(0).repeat(num_samples, 1, 1)

        comet_mask = torch.tensor(comet_mask, dtype=torch.float, device=device)
        comet_mask = comet_mask.unsqueeze(0).repeat(num_samples, 1, 1)

    generated = context
    with torch.no_grad():
        for _ in range(length):
            inputs = {"input_ids": generated}
            if comet_input is not None:
                inputs["comet_input"] = comet_input
                inputs["comet_mask"] = comet_mask
            if is_xlnet:
                # XLNet is a direct (predict same token, not next token) and bi-directional model by default
                # => need one additional dummy token in the input (will be masked), attention mask and target mapping (see model docstring)
                input_ids = torch.cat((generated, torch.zeros((1, 1), dtype=torch.long, device=device)), dim=1)
                perm_mask = torch.zeros((1, input_ids.shape[1], input_ids.shape[1]), dtype=torch.float, device=device)
                perm_mask[:, :, -1] = 1.0  # Previous tokens don't see last token
                target_mapping = torch.zeros((1, 1, input_ids.shape[1]), dtype=torch.float, device=device)
                target_mapping[0, 0, -1] = 1.0  # predict last token
                inputs = {"input_ids": input_ids, "perm_mask": perm_mask, "target_mapping": target_mapping}

            outputs = model(
                **inputs
            )  # Note: we could also use 'past' with GPT-2/Transfo-XL/XLNet (cached hidden-states)
            next_token_logits = outputs[0][0, -1, :] / temperature
            filtered_logits = top_k_top_p_filtering(next_token_logits, top_k=top_k, top_p=top_p)
            next_token = torch.multinomial(F.softmax(filtered_logits, dim=-1), num_samples=num_samples)
            generated = torch.cat((generated, next_token.unsqueeze(0)), dim=1)
    return generated


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model_type",
        default=None,
        type=str,
        required=True,
        help="Model type selected in the list: " + ", ".join(MODEL_CLASSES.keys()),
    )
    parser.add_argument(
        "--model_name_or_path",
        default=None,
        type=str,
        required=True,
        help="Path to pre-trained model or shortcut name selected in the list: " + ", ".join(ALL_MODELS),
    )
    parser.add_argument("--input-file", type=str, default=None, help="File to load instance prompts from")
    parser.add_argument(
        "--task",
        type=str,
        default=None,
        help="Which task for file input. If None, prompt is read as raw text 1 prompt per line in input-file",
    )
    parser.add_argument("--output-file", type=str, default=None, help="File to load instance prompts from")
    parser.add_argument("--prompt", type=str, default="")
    parser.add_argument("--padding_text", type=str, default="")
    parser.add_argument("--length", type=int, default=20)
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--top_k", type=int, default=0)
    parser.add_argument("--top_p", type=float, default=0.9)
    parser.add_argument("--no_cuda", action="store_true", help="Avoid using CUDA when available")
    parser.add_argument("--seed", type=int, default=42, help="random seed for initialization")

    parser.add_argument("--include_comet", default=False, type=bool, help="To include comet predictions or not")
    parser.add_argument(
        "--comet_model_path", default="comet-model/atomic_pretrained_model.th", type=str, help="Comet model path"
    )
    parser.add_argument("--comet_vocab_path", default="comet-vocab/", type=str, help="Comet model path")
    parser.add_argument("--comet_as_text", default=False, type=bool, help="Comet feature encoded using text")
    parser.add_argument(
        "--restrict_comet", default=False, type=bool, help="Restrict comet features to only o1's effect and o2's causes"
    )
    parser.add_argument("--num_samples", default=1, type=int, help="No. of samples to obtain.")

    args = parser.parse_args()

    args.device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
    args.n_gpu = torch.cuda.device_count()

    set_seed(args)

    args.model_type = args.model_type.lower()
    model_class, tokenizer_class = MODEL_CLASSES[args.model_type]
    tokenizer = tokenizer_class.from_pretrained(args.model_name_or_path)
    model = model_class.from_pretrained(args.model_name_or_path)
    model.to(args.device)

    comet_text_encoder = None
    if args.include_comet and not args.comet_as_text:
        logging.info("Setting comet model")
        opt, state_dict, vocab = comet_interactive.load_model_file(args.comet_model_path)
        # print(opt)
        comet_data_loader, comet_text_encoder = comet_interactive.load_data("atomic", opt, vocab, args.comet_vocab_path)

        n_ctx = comet_data_loader.max_event + comet_data_loader.max_effect
        n_vocab = len(comet_text_encoder.encoder) + n_ctx
        if not torch.cuda.is_available():
            comet_interactive.set_compute_mode("cpu")
        comet_model = comet_interactive.make_model(opt, n_vocab, n_ctx, state_dict)
        model.set_comet_model(comet_model)
        model.set_comet_encoder(comet_text_encoder)

    model.eval()

    if args.length < 0 and model.config.max_position_embeddings > 0:
        args.length = model.config.max_position_embeddings
    elif 0 < model.config.max_position_embeddings < args.length:
        args.length = model.config.max_position_embeddings  # No generation bigger than model size
    elif args.length < 0:
        args.length = MAX_LENGTH  # avoid infinite loop

    print(args)

    def _prompt_to_gen(txt, comet_event_inputs, comet_attention_masks):
        if args.model_type in ["transfo-xl", "xlnet"]:
            # Models with memory likes to have a long prompt for short inputs.
            txt = (args.padding_text if args.padding_text else PADDING_TEXT) + txt
        context_tokens = tokenizer.encode(txt)
        out = sample_sequence(
            model=model,
            context=context_tokens,
            length=args.length,
            temperature=args.temperature,
            top_k=args.top_k,
            top_p=args.top_p,
            device=args.device,
            is_xlnet=bool(args.model_type == "xlnet"),
            comet_input=comet_event_inputs,
            comet_mask=comet_attention_masks,
            num_samples=args.num_samples,
        )
        out = out[0, len(context_tokens) :].tolist()
        text = tokenizer.decode(out, clean_up_tokenization_spaces=True)
        return text

    if args.input_file is None:
        while True:
            raw_text = args.prompt if args.prompt else input("Model prompt >>> ")
            text = _prompt_to_gen(raw_text)
            print(text)
            if args.prompt:
                break
    else:
        if args.task is None:
            lines = read_lines(args.input_file)
            generations = []
            for l in lines:
                generations.append(_prompt_to_gen(l))
            write_items(generations, args.output_file)
        elif args.task == "anli":
            records = read_jsonl_lines(args.input_file)
            idx = 0
            for record in tqdm.tqdm(records):
                input_text_tokens = None
                comet_event_inputs = None
                comet_attention_masks = None

                if args.model_type == "gpt2_for_anli_comet":
                    (
                        input_text_tokens,
                        comet_event_inputs,
                        comet_attention_masks,
                    ) = record_to_text_tokens_with_comet_pred(
                        tokenizer=tokenizer,
                        record=record,
                        is_eval=True,
                        comet_as_text=args.comet_as_text,
                        include_comet=args.include_comet,
                        comet_text_encoder=comet_text_encoder,
                        restrict_comet=args.restrict_comet,
                    )
                elif args.model_type == "gpt2_for_anli":
                    input_text_tokens = anli_record_to_gpt_prompt(tokenizer=tokenizer, record=record, is_eval=True)

                input_text = " ".join(input_text_tokens)
                gen = _prompt_to_gen(input_text, comet_event_inputs, comet_attention_masks)
                if args.model_type == "gpt2_for_anli":
                    period_idx = gen.find(".")
                    if period_idx != -1:
                        gen = gen[:period_idx]

                if "generations" not in record:
                    record["generations"] = {}
                record["generations"][args.model_type] = [gen]

                if idx < 5:
                    print("Input context format: {}".format(input_text_tokens))
                    if comet_event_inputs is not None:
                        print("Comet event input format: {}".format(comet_event_inputs))
                        print("Comet mask: {}".format(comet_attention_masks))
                idx += 1
            write_items([json.dumps(r) for r in records], args.output_file)


if __name__ == "__main__":
    main()
