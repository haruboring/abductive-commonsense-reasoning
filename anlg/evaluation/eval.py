import json
import sys
from argparse import ArgumentParser
from collections import defaultdict

from anlg.evaluation.bert_score.bert_score import BertScore
from anlg.evaluation.bleu.bleu import Bleu
from anlg.evaluation.cider.cider import Cider
from anlg.evaluation.meteor.meteor_nltk import Meteor
from anlg.evaluation.rouge.rouge import Rouge

# reload(sys)
# sys.setdefaultencoding('utf-8')


class QGEvalCap:
    # gts: 正解, res: 生成されたやつ
    def __init__(self, model_key, gts, res, results_file):
        self.gts = gts
        self.res = res
        self.results_file = results_file
        self.model_key = model_key

    def evaluate(self):
        output = []
        scorers = [
            (Bleu(4), ["Bleu_1", "Bleu_2", "Bleu_3", "Bleu_4"]),
            (Meteor(), "METEOR"),
            (Rouge(), "ROUGE_L"),
            (Cider(), "CIDEr"),
            (BertScore(), "Bert Score"),
        ]

        # =================================================
        # Compute scores
        # =================================================
        scores_dict = {}
        scores_dict["model_key"] = self.model_key
        for scorer, method in scorers:
            # print 'computing %s score...'%(scorer.method())
            score, scores = scorer.compute_score(self.gts, self.res)
            if type(method) == list:
                for sc, scs, m in zip(score, scores, method):
                    print("%s: %0.5f" % (m, sc))
                    output.append(sc)
                    scores_dict[m] = str(sc)
            else:
                print("%s: %0.5f" % (method, score))
                output.append(score)
                scores_dict[method] = score

        with open(self.results_file, "a") as f:
            f.write(json.dumps(scores_dict) + "\n")

        return output


def eval(model_key, sources, references, predictions, results_file):
    # source: (obs1, obs2), reference: hyp（正解）, prediction: generation（生成されたやつ）
    """
    Given a filename, calculate the metric scores for that prediction file
    isDin: boolean value to check whether input file is DirectIn.txt
    """

    pairs = []

    for tup in sources:
        pair = {}
        pair["tokenized_sentence"] = tup
        pairs.append(pair)

    cnt = 0
    for line in references:
        pairs[cnt]["tokenized_question"] = line
        cnt += 1

    output = predictions

    for idx, pair in enumerate(pairs):
        pair["prediction"] = output[idx]

    ## eval
    import json
    from json import encoder

    from anlg.evaluation.eval import QGEvalCap

    encoder.FLOAT_REPR = lambda o: format(o, ".4f")

    res = defaultdict(lambda: [])
    gts = defaultdict(lambda: [])
    for pair in pairs[:]:
        key = pair["tokenized_sentence"]
        # res[key] = [pair['prediction']]
        res[key] = pair["prediction"]

        ## gts
        # 正解
        gts[key].append(pair["tokenized_question"])

    # gts: 正解, res: 生成されたやつ
    QGEval = QGEvalCap(model_key, gts, res, results_file)
    return QGEval.evaluate()


def preprocess(file_name, keys):
    with open(file_name) as f:
        data = f.readlines()
        generations = [json.loads(elem) for elem in data]

    predictions = {}
    references = {}
    sources = {}
    keys_list = keys if keys != None else generations[0]["generations"].keys()
    for key in keys_list:
        references[key] = []
        predictions[key] = []
        sources[key] = []

    for elem in generations:
        label = elem["label"]
        hyp = elem["hyp" + label]
        for key in keys_list:
            if key in elem["generations"]:
                references[key].append(hyp)
                predictions[key].append(elem["generations"][key])
                sources[key].append((elem["obs1"], elem["obs2"]))

    # source: (obs1, obs2), reference: hyp（正解）, prediction: generation（生成されたやつ）
    return sources, references, predictions


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("-gen_file", "--gen_file", dest="gen_file", help="generations file with gold/references")
    parser.add_argument("--keys", type=str, default=None, help="comma-separated list of model keys")
    parser.add_argument("--results_file", default="eval_results.jsonl")
    args = parser.parse_args()

    print("scores: \n")
    keys = None
    if args.keys:
        keys = args.keys.split(",")

    sources, references, predictions = preprocess(args.gen_file, keys)
    for key in references.keys():
        print("\nEvaluating %s" % key)
        eval(key, sources[key], references[key], predictions[key], args.results_file)
